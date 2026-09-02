"""Unit tests for event clustering, fingerprint extraction, and material update detection."""

from unittest.mock import AsyncMock, patch

import pytest

from src.api.models.article import ArticleIn
from src.api.models.cluster import EventFingerprint
from src.api.services.cluster import (
    ClusterService,
    compute_normalized_title_hash,
    extract_heuristic_fingerprint,
)


def test_compute_normalized_title_hash_identical_for_reordered_or_punctuated_titles() -> None:
    """Tests that normalized title hash is invariant to punctuation, casing, and stopwords."""
    title1 = "OpenAI releases GPT-5 with incredible new reasoning capabilities!"
    title2 = "OpenAI Releases GPT-5 with Incredible New Reasoning Capabilities"
    title3 = "GPT-5: OpenAI releases new incredible reasoning capabilities."

    hash1 = compute_normalized_title_hash(title1)
    hash2 = compute_normalized_title_hash(title2)
    hash3 = compute_normalized_title_hash(title3)

    assert hash1 == hash2 == hash3


def test_extract_heuristic_fingerprint() -> None:
    """Tests heuristic fingerprint extraction from title."""
    title = "SpaceX Launches Starship Flight 6 from Texas"
    entity, action = extract_heuristic_fingerprint(title)
    assert "SpaceX" in entity or "Starship" in entity
    assert len(action) > 0


@pytest.mark.asyncio
async def test_extract_fingerprint_with_llm() -> None:
    """Tests extracting fingerprint via LLM completion with mock."""
    mock_llm_json = '{"entity": "OpenAI", "action": "releases GPT-5 model"}'
    article = ArticleIn(
        title="OpenAI Officially Launches GPT-5",
        link="https://example.com/openai-gpt5",
        content="OpenAI has released its next generation AI model GPT-5 today.",
    )

    with patch(
        "src.api.services.cluster.generate_completion", return_value=mock_llm_json
    ) as mock_llm:
        service = ClusterService()
        fp = await service.extract_fingerprint(article)

        assert fp.entity == "OpenAI"
        assert fp.action == "releases GPT-5 model"
        assert len(fp.normalized_title_hash) == 12
        mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_cluster_articles_collapses_same_event_stories() -> None:
    """Tests that articles covering the same event collapse into a single cluster."""
    mock_repo = AsyncMock()
    mock_repo.find_recent_clusters.return_value = []
    mock_repo.create_cluster.return_value = 1
    mock_repo.add_article_to_cluster.return_value = None

    articles = [
        ArticleIn(
            title="OpenAI Releases GPT-5 AI Model",
            link="https://tech.com/gpt5-1",
            content="OpenAI launched GPT-5 today.",
            source="TechCrunch",
        ),
        ArticleIn(
            title="GPT-5 Released by OpenAI with Enhanced Reasoning",
            link="https://news.com/gpt5-2",
            content="Today OpenAI released GPT-5.",
            source="The Verge",
        ),
        ArticleIn(
            title="Apple Announces New MacBook Pro with M4",
            link="https://apple.com/mbp-m4",
            content="Apple announced new M4 laptops.",
            source="AppleInsider",
        ),
    ]

    service = ClusterService(repository=mock_repo)

    # Patch fingerprint extraction to return matching entities/actions for OpenAI
    async def mock_extract(art: ArticleIn) -> EventFingerprint:
        if "GPT-5" in art.title:
            return EventFingerprint(
                entity="OpenAI",
                action="releases GPT-5 model",
                normalized_title_hash=compute_normalized_title_hash(art.title),
            )
        return EventFingerprint(
            entity="Apple",
            action="announces MacBook Pro M4",
            normalized_title_hash=compute_normalized_title_hash(art.title),
        )

    with patch.object(service, "extract_fingerprint", side_effect=mock_extract):
        clusters = await service.cluster_articles(articles)

        assert len(clusters) == 2
        # First cluster has 2 articles (OpenAI)
        openai_cluster = next(c for c in clusters if c.fingerprint.entity == "OpenAI")
        assert len(openai_cluster.articles) == 2
        # Second cluster has 1 article (Apple)
        apple_cluster = next(c for c in clusters if c.fingerprint.entity == "Apple")
        assert len(apple_cluster.articles) == 1


@pytest.mark.asyncio
async def test_material_update_detection() -> None:
    """Tests material update detection when new facts arrive."""
    mock_llm_update = (
        '{"is_material_update": true, "reason": "Official confirmation and pricing revealed."}'
    )
    service = ClusterService()

    existing = [
        {"title": "Rumors of GPT-5 Announcement", "content": "Rumors suggest GPT-5 is coming."}
    ]
    new_art = ArticleIn(
        title="OpenAI Officially Releases GPT-5 with Pricing",
        link="https://example.com/gpt5-official",
        content="OpenAI officially announced GPT-5 availability at $20/month.",
    )

    with patch("src.api.services.cluster.generate_completion", return_value=mock_llm_update):
        is_material = await service.check_material_update(existing, new_art)
        assert is_material is True
