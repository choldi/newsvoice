"""Integration tests for POST /cluster endpoint with related and unrelated articles."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.cluster import EventFingerprint
from src.api.services.cluster import compute_normalized_title_hash

client = TestClient(app)


@pytest.fixture
def sample_15_articles_fixture() -> list[dict[str, str]]:
    """Fixture with 10 related articles (5 OpenAI, 3 Apple, 2 SpaceX) + 5 unrelated articles."""
    return [
        # 5 Related to OpenAI GPT-5
        {
            "title": "OpenAI Launches GPT-5 with Advanced Autonomous Reasoning",
            "link": "https://techcrunch.com/openai-gpt-5-launch",
            "content": "OpenAI announced GPT-5 today with massive improvements in reasoning.",
            "source": "TechCrunch",
        },
        {
            "title": "GPT-5 Released by OpenAI in Major Artificial Intelligence Leap",
            "link": "https://theverge.com/gpt-5-released-openai",
            "content": "Today OpenAI released GPT-5 featuring state of the art benchmarks.",
            "source": "The Verge",
        },
        {
            "title": "OpenAI Unveils GPT-5 AI Model to General Public",
            "link": "https://reuters.com/technology/openai-gpt5-unveiled",
            "content": "OpenAI unveiled GPT-5, marking its next generation frontier AI system.",
            "source": "Reuters",
        },
        {
            "title": "OpenAI's New GPT-5 Model Arrives with Enhanced Logic",
            "link": "https://bbc.com/news/technology-openai-gpt5",
            "content": "BBC reports on the launch of GPT-5 by OpenAI.",
            "source": "BBC News",
        },
        {
            "title": "Silicon Valley Reacts as OpenAI Launches GPT-5",
            "link": "https://nytimes.com/tech/openai-gpt5-reaction",
            "content": (
                "Industry leaders reacted enthusiastically as OpenAI officially released GPT-5."
            ),
            "source": "NYT",
        },
        # 3 Related to Apple M4 MacBook Pro
        {
            "title": "Apple Announces M4 MacBook Pro with Thunderbolt 5",
            "link": "https://bloomberg.com/apple-m4-macbook-pro",
            "content": "Apple introduced its updated MacBook Pro lineup powered by M4 chips.",
            "source": "Bloomberg",
        },
        {
            "title": "New Apple MacBook Pro Features M4 Max and M4 Pro",
            "link": "https://9to5mac.com/apple-macbook-pro-m4-max",
            "content": "Apple unveiled the high performance M4 Max and M4 Pro MacBook Pro.",
            "source": "9to5Mac",
        },
        {
            "title": "Apple M4 MacBook Pro Lineup Officially Revealed",
            "link": "https://macrumors.com/apple-m4-macbook-pro-revealed",
            "content": "Apple has refreshed the MacBook Pro line with M4 processors.",
            "source": "MacRumors",
        },
        # 2 Related to SpaceX Starship
        {
            "title": "SpaceX Successfully Launches Starship Flight 6",
            "link": "https://space.com/spacex-starship-flight-6-launch",
            "content": "SpaceX completed another orbital test flight of Starship.",
            "source": "Space.com",
        },
        {
            "title": "Starship Flight 6 Achieves Key Test Milestones in Orbit",
            "link": "https://arstechnica.com/space/starship-flight-6-success",
            "content": "SpaceX Starship flight 6 performed experimental reignitions in space.",
            "source": "Ars Technica",
        },
        # 5 Unrelated Articles
        {
            "title": "Federal Reserve Cuts Interest Rates by 25 Basis Points",
            "link": "https://wsj.com/economy/fed-rate-cut-25bps",
            "content": "The Federal Reserve lowered benchmark interest rates today.",
            "source": "WSJ",
        },
        {
            "title": "James Webb Telescope Discovers Atmospheric Water on Exoplanet",
            "link": "https://nasa.gov/news/jwst-exoplanet-water",
            "content": "Astronomers detected water vapor in a distant exoplanet atmosphere.",
            "source": "NASA",
        },
        {
            "title": "Real Madrid Wins UEFA Champions League Match 3-1",
            "link": "https://espn.com/soccer/real-madrid-victory",
            "content": "Real Madrid secured a 3-1 win in European competition.",
            "source": "ESPN",
        },
        {
            "title": "Global Climate Summit Concludes with Historic Renewable Pact",
            "link": "https://theguardian.com/environment/climate-summit-accord",
            "content": "World leaders reached an agreement to triple renewable energy by 2030.",
            "source": "The Guardian",
        },
        {
            "title": "Nobel Prize in Medicine Awarded for MicroRNA Breakthrough",
            "link": "https://nobelprize.org/medicine-micro-rna",
            "content": (
                "The Nobel Assembly awarded the prize for discoveries in microRNA gene regulation."
            ),
            "source": "Nobel Prize",
        },
    ]


def test_cluster_endpoint_with_15_articles_fixture(
    sample_15_articles_fixture: list[dict[str, str]],
) -> None:
    """Tests POST /cluster with 10 related + 5 unrelated articles collapsing into 8 clusters."""

    # Mock LLM fingerprint extraction to return structured event entities & actions
    def mock_extract(art):
        title = art.title
        if "GPT-5" in title or "OpenAI" in title:
            return EventFingerprint(
                entity="OpenAI",
                action="launches GPT-5 reasoning model",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "MacBook Pro" in title or "M4" in title:
            return EventFingerprint(
                entity="Apple",
                action="announces M4 MacBook Pro lineup",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "Starship" in title or "SpaceX" in title:
            return EventFingerprint(
                entity="SpaceX",
                action="launches Starship Flight 6",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "Federal Reserve" in title:
            return EventFingerprint(
                entity="Federal Reserve",
                action="cuts interest rates",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "James Webb" in title:
            return EventFingerprint(
                entity="NASA JWST",
                action="discovers water on exoplanet",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "Real Madrid" in title:
            return EventFingerprint(
                entity="Real Madrid",
                action="wins UEFA match",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "Climate Summit" in title:
            return EventFingerprint(
                entity="Global Climate Summit",
                action="concludes renewable energy pact",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        elif "Nobel Prize" in title:
            return EventFingerprint(
                entity="Nobel Assembly",
                action="awards medicine prize for microRNA",
                normalized_title_hash=compute_normalized_title_hash(title),
            )
        return EventFingerprint(
            entity="General",
            action="news event",
            normalized_title_hash=compute_normalized_title_hash(title),
        )

    with patch(
        "src.api.services.cluster.ClusterService.extract_fingerprint",
        side_effect=mock_extract,
    ):
        response = client.post("/cluster", json={"articles": sample_15_articles_fixture})
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["total_articles"] == 15
        assert data["total_clusters"] == 8

        # Verify 5 OpenAI articles collapsed into 1 cluster
        openai_clusters = [c for c in data["clusters"] if c["fingerprint"]["entity"] == "OpenAI"]
        assert len(openai_clusters) == 1
        assert len(openai_clusters[0]["articles"]) == 5

        # Verify 3 Apple articles collapsed into 1 cluster
        apple_clusters = [c for c in data["clusters"] if c["fingerprint"]["entity"] == "Apple"]
        assert len(apple_clusters) == 1
        assert len(apple_clusters[0]["articles"]) == 3

        # Verify 2 SpaceX articles collapsed into 1 cluster
        spacex_clusters = [c for c in data["clusters"] if c["fingerprint"]["entity"] == "SpaceX"]
        assert len(spacex_clusters) == 1
        assert len(spacex_clusters[0]["articles"]) == 2


def test_summarize_endpoint_with_clustered_input() -> None:
    """Tests POST /summarize endpoint consuming pre-grouped clusters."""
    payload = {
        "clusters": [
            {
                "cluster_id": 1,
                "fingerprint": {
                    "entity": "OpenAI",
                    "action": "launches GPT-5",
                    "normalized_title_hash": "abc123def456",
                },
                "articles": [
                    {
                        "title": "OpenAI Releases GPT-5",
                        "link": "https://example.com/gpt5-1",
                        "source": "TechCrunch",
                    },
                    {
                        "title": "GPT-5 Out Now",
                        "link": "https://example.com/gpt5-2",
                        "source": "The Verge",
                    },
                ],
                "is_material_update": True,
            }
        ]
    }

    mock_markdown = (
        "## Top 5 Main Stories\n"
        "- **OpenAI launches GPT-5**: Major AI milestone released across 2 sources.\n\n"
        "## Top 5 Quick Headlines\n"
        "- Quick headline bullet."
    )

    with patch("src.api.services.digest.generate_completion", return_value=mock_markdown):
        response = client.post("/summarize", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["article_count"] == 2
        assert "OpenAI launches GPT-5" in data["digest"]
