"""Unit tests for src/api/services/digest.py."""

from unittest.mock import patch

import pytest

from src.api.models.article import ArticleIn, DigestIn
from src.api.models.cluster import ClusterItem, Fingerprint
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


@pytest.mark.asyncio
async def test_digest_service_injects_language() -> None:
    """Digest service passes the per-podcast language into the system prompt."""
    service = DigestService()
    articles = [
        ArticleIn(
            title="Tech News 1",
            link="https://example.com/1",
            content="Summary 1",
            source="Tech",
        ),
    ]
    payload = DigestIn(articles=articles, language="es")

    with patch("src.api.services.digest.generate_completion", return_value="# Digest") as mock_llm:
        await service.generate_digest(payload)

    sent_messages = mock_llm.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "Language requirement" in system_content
    assert "es" in system_content


@pytest.mark.asyncio
async def test_digest_service_injects_prompt_template() -> None:
    """Per-collection templates are injected into the system prompt."""
    service = DigestService()
    articles = [
        ArticleIn(title="Finance News", link="https://example.com/1", source="Bloomberg"),
    ]
    payload = DigestIn(articles=articles, prompt_template="finance", collection="finance")

    with patch("src.api.services.digest.generate_completion", return_value="# Digest") as mock_llm:
        await service.generate_digest(payload)

    sent_messages = mock_llm.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "Finance-specific requirements" in system_content


@pytest.mark.asyncio
async def test_digest_service_unknown_template_falls_back() -> None:
    """An unknown template name keeps the base prompt."""
    service = DigestService()
    articles = [ArticleIn(title="Tech News", link="https://example.com/2", source="Tech")]
    payload = DigestIn(articles=articles, prompt_template="does-not-exist")

    with patch("src.api.services.digest.generate_completion", return_value="# Digest") as mock_llm:
        await service.generate_digest(payload)

    sent_messages = mock_llm.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "Finance-specific requirements" not in system_content


@pytest.mark.asyncio
async def test_digest_service_injects_podcast_name() -> None:
    """Podcast name is injected into the system prompt."""
    service = DigestService()
    articles = [
        ArticleIn(
            title="Tech News 1",
            link="https://example.com/1",
            content="Summary 1",
            source="Tech",
        ),
    ]
    payload = DigestIn(articles=articles, podcast_name="ByteCast")

    with patch("src.api.services.digest.generate_completion", return_value="# Digest") as mock_llm:
        await service.generate_digest(payload)

    sent_messages = mock_llm.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "Podcast identity" in system_content
    assert "This is ByteCast" in system_content


@pytest.mark.asyncio
async def test_generate_selection_logic_uses_dedicated_prompt() -> None:
    """The logic call uses its own system prompt and returns the LLM text."""
    service = DigestService()
    cluster = ClusterItem(
        fingerprint=Fingerprint(entity="ACME", action="Layoffs", title_hash="abc"),
        articles=[ArticleIn(title="ACME Layoffs", link="https://example.com/1")],
    )
    digest = "# Daily Digest\n\n## Section 1: Top 5 Main Stories"

    with patch(
        "src.api.services.digest.generate_completion", return_value="Se eligio ACME por impacto."
    ) as mock_llm:
        logic = await service.generate_selection_logic(
            clusters=[cluster], digest=digest, prompt_template="default", language="es"
        )

    assert logic == "Se eligio ACME por impacto."
    messages = mock_llm.call_args.kwargs["messages"]
    assert "news selection logic" in messages[0]["content"]
    user_prompt = messages[1]["content"]
    assert "Story Cluster" in user_prompt
    assert "ACME Layoffs" in user_prompt
    assert digest in user_prompt
    assert "Language: es" in user_prompt


@pytest.mark.asyncio
async def test_generate_selection_logic_uses_template_priority_rules() -> None:
    """Health/Finance templates inject their specific selection rules."""
    service = DigestService()
    cluster = ClusterItem(
        fingerprint=Fingerprint(entity="FDA", action="Approves drug", title_hash="def"),
        articles=[ArticleIn(title="FDA approvals", link="https://example.com/2")],
    )

    with patch("src.api.services.digest.generate_completion", return_value="Logic") as mock_llm:
        await service.generate_selection_logic(
            clusters=[cluster], digest="# D", prompt_template="health"
        )

    user_prompt = mock_llm.call_args.kwargs["messages"][1]["content"]
    assert "Public health impact" in user_prompt


@pytest.mark.asyncio
async def test_generate_selection_logic_empty_clusters() -> None:
    """An empty cluster list still yields a valid logic call."""
    service = DigestService()

    with patch("src.api.services.digest.generate_completion", return_value="Logic") as mock_llm:
        logic = await service.generate_selection_logic(clusters=[], digest="# D")

    assert logic == "Logic"
    mock_llm.assert_called_once()
