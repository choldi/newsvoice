"""Integration tests for /cluster endpoint."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def fake_fingerprint_by_title(title: str) -> str:
    """Return a deterministic fingerprint JSON based on the title keywords."""
    if "OpenAI" in title or "GPT" in title:
        return '{"entity": "OpenAI", "action": "releases GPT-5"}'
    if "iPhone" in title:
        return '{"entity": "Apple", "action": "announces iPhone 16"}'
    return '{"entity": "Google", "action": "launches Gemini 2.0"}'


def _fake_batch_fingerprint_side_effect(messages: list[dict], **kwargs: object) -> str:
    """Return a batch fingerprint JSON keyed by item number for the batched prompt."""
    items_section = messages[1]["content"].split("\n\n", 1)[1]
    out: dict[str, str] = {}
    for line in items_section.splitlines():
        line = line.strip()
        if not line or ". Title:" not in line:
            continue
        number, _, title = line.partition(". Title:")
        title = title.split(" | Content:")[0].strip()
        out[number.strip()] = json.loads(fake_fingerprint_by_title(title))
    return json.dumps(out)


def test_cluster_endpoint_success() -> None:
    """Test /cluster endpoint with multiple articles."""
    payload = {
        "articles": [
            {
                "title": "OpenAI releases GPT-5 with reasoning",
                "link": "https://example.com/gpt5-1",
                "content": "OpenAI announced GPT-5 today with advanced reasoning.",
                "source": "TechCrunch",
            },
            {
                "title": "GPT-5 launched by OpenAI, new features",
                "link": "https://example.com/gpt5-2",
                "content": "The new GPT-5 model includes reasoning capabilities.",
                "source": "The Verge",
            },
            {
                "title": "Apple unveils iPhone 16 with AI chip",
                "link": "https://example.com/iphone16",
                "content": "Apple announced iPhone 16 with new neural engine.",
                "source": "MacRumors",
            },
        ]
    }
    with patch(
        "src.api.services.cluster.generate_completion",
        side_effect=_fake_batch_fingerprint_side_effect,
    ):
        response = client.post("/api/cluster", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "clusters" in data
    assert data["total_articles"] == 3
    # Should create 2 clusters: OpenAI GPT-5 (2 articles) + Apple iPhone 16
    assert len(data["clusters"]) == 2


def test_cluster_endpoint_empty_payload() -> None:
    """Test /cluster endpoint with empty articles list."""
    payload = {"articles": []}
    response = client.post("/api/cluster", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["clusters"] == []
    assert data["total_articles"] == 0


def test_cluster_endpoint_invalid_payload() -> None:
    """Test /cluster endpoint with invalid payload."""
    payload = {"articles": [{"title": "Missing link"}]}
    response = client.post("/api/cluster", json=payload)
    assert response.status_code == 422  # Validation error


def test_cluster_endpoint_related_articles_same_cluster() -> None:
    """Test that related articles about same event are clustered together."""
    payload = {
        "articles": [
            {
                "title": "OpenAI releases GPT-5 with reasoning capabilities",
                "link": "https://example.com/gpt5-a",
                "content": "OpenAI announced GPT-5 today...",
                "source": "TechCrunch",
            },
            {
                "title": "GPT-5 launched by OpenAI, features advanced reasoning",
                "link": "https://example.com/gpt5-b",
                "content": "The new GPT-5 model includes...",
                "source": "The Verge",
            },
            {
                "title": "OpenAI GPT-5 release: reasoning and multimodal",
                "link": "https://example.com/gpt5-c",
                "content": "GPT-5 brings reasoning to ChatGPT...",
                "source": "Ars Technica",
            },
            {
                "title": "Apple announces iPhone 16 with new AI chip",
                "link": "https://example.com/iphone16",
                "content": "Apple revealed iPhone 16...",
                "source": "MacRumors",
            },
            {
                "title": "Google launches Gemini 2.0 with agent features",
                "link": "https://example.com/gemini2",
                "content": "Google announced Gemini 2.0...",
                "source": "TechCrunch",
            },
        ]
    }

    with patch(
        "src.api.services.cluster.generate_completion",
        side_effect=_fake_batch_fingerprint_side_effect,
    ):
        response = client.post("/api/cluster", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["total_articles"] == 5
    # Should have 3 clusters: OpenAI GPT-5 (3 articles), Apple iPhone 16, Google Gemini 2.0
    assert len(data["clusters"]) == 3
    # Find the OpenAI cluster
    openai_cluster = next(
        (c for c in data["clusters"] if "OpenAI" in c["fingerprint"]["entity"]), None
    )
    assert openai_cluster is not None
    assert len(openai_cluster["articles"]) == 3
    assert openai_cluster["is_material_update"] is True
