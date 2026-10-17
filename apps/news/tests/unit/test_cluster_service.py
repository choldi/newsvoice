"""Unit tests for ClusterService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.models.article import ArticleIn
from src.api.models.cluster import ClusterIn, Fingerprint
from src.api.services.cluster import ClusterService


@pytest.fixture
def mock_repo() -> MagicMock:
    """Mock ArticleRepository."""
    repo = MagicMock()
    repo.find_recent_clusters = AsyncMock(return_value=[])
    repo.save_articles = AsyncMock()
    repo.create_cluster = AsyncMock(return_value=1)
    repo.add_article_to_cluster = AsyncMock()
    repo.update_cluster_last_seen = AsyncMock()
    repo.get_cluster_articles = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def cluster_service(mock_repo: MagicMock) -> ClusterService:
    """ClusterService with mocked repository."""
    return ClusterService(repository=mock_repo)


@pytest.fixture
def sample_articles() -> list[ArticleIn]:
    """Sample articles for testing."""
    return [
        ArticleIn(
            title="OpenAI releases GPT-5 with reasoning capabilities",
            link="https://example.com/openai-gpt5",
            content="OpenAI announced GPT-5 today...",
            source="TechCrunch",
        ),
        ArticleIn(
            title="GPT-5 launched by OpenAI, new reasoning features",
            link="https://example.com/gpt5-launch",
            content="The new model from OpenAI...",
            source="The Verge",
        ),
        ArticleIn(
            title="Apple announces iPhone 16 with AI chips",
            link="https://example.com/apple-iphone16",
            content="Apple revealed the iPhone 16...",
            source="MacRumors",
        ),
    ]


class TestClusterService:
    """Tests for ClusterService."""

    @pytest.mark.asyncio
    async def test_normalize_title_removes_stopwords_and_numbers(
        self, cluster_service: ClusterService
    ) -> None:
        """Test title normalization removes stopwords, punctuation, numbers."""
        title = "OpenAI releases GPT-5 with NEW reasoning capabilities!"
        normalized = cluster_service._normalize_title(title)
        # Should remove: "with", "new", "gpt", "5", punctuation
        assert "openai" in normalized
        assert "releases" in normalized
        assert "reasoning" in normalized
        assert "capabilities" in normalized
        assert "with" not in normalized
        assert "new" not in normalized
        assert "gpt" not in normalized
        assert "5" not in normalized

    @pytest.mark.asyncio
    async def test_compute_title_hash_deterministic(self, cluster_service: ClusterService) -> None:
        """Test title hash is deterministic and short."""
        title = "OpenAI releases GPT-5"
        hash1 = cluster_service._compute_title_hash(title)
        hash2 = cluster_service._compute_title_hash(title)
        assert hash1 == hash2
        assert len(hash1) == 12  # SHA256 truncated to 12 chars

    @pytest.mark.asyncio
    async def test_fingerprint_to_key_consistent(self, cluster_service: ClusterService) -> None:
        """Test fingerprint key generation is consistent."""
        fp = Fingerprint(entity="OpenAI", action="releases GPT-5", title_hash="abc123")
        key1 = cluster_service._fingerprint_to_key(fp)
        key2 = cluster_service._fingerprint_to_key(fp)
        assert key1 == key2
        assert key1 == "openai|releases gpt-5"
        # title_hash must not influence the grouping key
        v2 = Fingerprint(entity="OpenAI", action="releases GPT-5", title_hash="xyz789")
        assert cluster_service._fingerprint_to_key(v2) == key1

    @pytest.mark.asyncio
    async def test_cluster_articles_empty_input(self, cluster_service: ClusterService) -> None:
        """Test clustering empty article list returns empty."""
        payload = ClusterIn(articles=[])
        result = await cluster_service.cluster_articles(payload)
        assert result == []

    @pytest.mark.asyncio
    async def test_cluster_articles_creates_new_clusters(
        self,
        cluster_service: ClusterService,
        mock_repo: MagicMock,
        sample_articles: list[ArticleIn],
    ) -> None:
        """Test related articles about the same event collapse into one cluster."""
        with patch.object(
            cluster_service, "_extract_fingerprints_batch", new_callable=AsyncMock
        ) as mock_extract:
            # First two articles share entity+action (same event)
            mock_extract.return_value = {
                0: Fingerprint(entity="OpenAI", action="releases GPT-5", title_hash="hash1"),
                1: Fingerprint(entity="OpenAI", action="releases GPT-5", title_hash="hash2"),
                2: Fingerprint(entity="Apple", action="announces iPhone 16", title_hash="hash3"),
            }

            payload = ClusterIn(articles=sample_articles)
            result = await cluster_service.cluster_articles(payload)

            assert len(result) == 2
            assert mock_repo.create_cluster.call_count == 2
            assert mock_repo.add_article_to_cluster.call_count == 2
            assert all(c.is_material_update for c in result)
            openai_cluster = next(c for c in result if c.fingerprint.entity == "OpenAI")
            assert len(openai_cluster.articles) == 2

    @pytest.mark.asyncio
    async def test_cluster_articles_matches_existing_cluster(
        self,
        cluster_service: ClusterService,
        mock_repo: MagicMock,
        sample_articles: list[ArticleIn],
    ) -> None:
        """Test articles matched to existing cluster by fingerprint."""
        # Pre-existing cluster in DB
        mock_repo.find_recent_clusters.return_value = [
            {
                "id": 42,
                "fingerprint": {
                    "entity": "OpenAI",
                    "action": "releases GPT-5",
                    "title_hash": "hash1",
                },
                "first_seen": "2024-01-01T00:00:00",
                "last_seen": "2024-01-01T00:00:00",
            }
        ]
        mock_repo.get_cluster_articles.return_value = [
            {"title": "Old article", "content": "Old content"}
        ]

        with patch.object(
            cluster_service, "_extract_fingerprints_batch", new_callable=AsyncMock
        ) as mock_extract:
            # First article matches existing cluster
            mock_extract.return_value = {
                0: Fingerprint(entity="OpenAI", action="releases GPT-5", title_hash="hash1"),
                1: Fingerprint(entity="Apple", action="announces iPhone 16", title_hash="hash3"),
            }

            payload = ClusterIn(articles=sample_articles[:2])
            result = await cluster_service.cluster_articles(payload)

            assert len(result) == 2
            # First cluster should be existing one (id=42)
            openai_cluster = next(c for c in result if c.fingerprint.entity == "OpenAI")
            assert openai_cluster.cluster_id == 42
            assert len(openai_cluster.articles) == 1
            # Second cluster should be new
            apple_cluster = next(c for c in result if c.fingerprint.entity == "Apple")
            assert apple_cluster.cluster_id != 42

    @pytest.mark.asyncio
    async def test_parse_fingerprint_pair_batch_list(self) -> None:
        """Test batch parser accepts a JSON list aligned to input order."""
        data = [
            {"entity": "OpenAI", "action": "releases GPT-5"},
            {"entity": "Apple", "action": "announces iPhone 16"},
        ]
        pairs = ClusterService._parse_fingerprint_pair_batch(data, expected=2)
        assert pairs == {
            0: ("OpenAI", "releases GPT-5"),
            1: ("Apple", "announces iPhone 16"),
        }

    @pytest.mark.asyncio
    async def test_parse_fingerprint_pair_batch_object_1based(self) -> None:
        """Test batch parser accepts a JSON object keyed by 1-based numbers."""
        data = {
            "1": {"entity": "OpenAI", "action": "releases GPT-5"},
            "2": {"entity": "Apple", "action": "announces iPhone 16"},
        }
        pairs = ClusterService._parse_fingerprint_pair_batch(data, expected=2)
        assert pairs == {
            0: ("OpenAI", "releases GPT-5"),
            1: ("Apple", "announces iPhone 16"),
        }

    @pytest.mark.asyncio
    async def test_parse_fingerprint_pair_batch_missing_entries_unknown(self) -> None:
        """Test missing/malformed entries fall back to Unknown."""
        data = [{"entity": "OpenAI", "action": "releases GPT-5"}, "garbage"]
        pairs = ClusterService._parse_fingerprint_pair_batch(data, expected=3)
        assert pairs[0] == ("OpenAI", "releases GPT-5")
        assert pairs[1] == ("Unknown", "Unknown")
        assert pairs[2] == ("Unknown", "Unknown")

    @pytest.mark.asyncio
    async def test_extract_fingerprints_batch_fallback_on_llm_error(
        self, cluster_service: ClusterService, sample_articles: list[ArticleIn]
    ) -> None:
        """Test batch extraction falls back to Unknown when the LLM call fails."""
        with patch(
            "src.api.services.cluster.generate_completion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_llm.side_effect = RuntimeError("LLM down")
            fingerprints = await cluster_service._extract_fingerprints_batch(sample_articles[:2])
            assert len(fingerprints) == 2
            for fp in fingerprints.values():
                assert fp.entity == "Unknown"
                assert fp.action == "Unknown"
                assert len(fp.title_hash) == 12

    @pytest.mark.asyncio
    async def test_check_material_update_true(
        self, cluster_service: ClusterService, mock_repo: MagicMock
    ) -> None:
        """Test material update detection returns True for significant new facts."""
        mock_repo.get_cluster_articles.return_value = [
            {"title": "OpenAI announces GPT-5", "content": "OpenAI will release GPT-5 soon."}
        ]

        with patch(
            "src.api.services.cluster.generate_completion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_llm.return_value = (
                '{"is_material_update": true, "reason": "Release date confirmed"}'
            )
            new_article = ArticleIn(
                title="OpenAI releases GPT-5 today",
                link="https://example.com/gpt5-released",
                content="GPT-5 is now available with reasoning.",
                source="TechCrunch",
            )
            is_update = await cluster_service._check_material_update(1, new_article)
            assert is_update is True

    @pytest.mark.asyncio
    async def test_check_material_update_false(
        self, cluster_service: ClusterService, mock_repo: MagicMock
    ) -> None:
        """Test material update detection returns False for re-reporting."""
        mock_repo.get_cluster_articles.return_value = [
            {"title": "OpenAI releases GPT-5", "content": "GPT-5 is now available."}
        ]

        with patch(
            "src.api.services.cluster.generate_completion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_llm.return_value = (
                '{"is_material_update": false, "reason": "Same facts re-reported"}'
            )
            new_article = ArticleIn(
                title="GPT-5 released by OpenAI",
                link="https://example.com/gpt5-released-2",
                content="OpenAI has released GPT-5.",
                source="The Verge",
            )
            is_update = await cluster_service._check_material_update(1, new_article)
            assert is_update is False
