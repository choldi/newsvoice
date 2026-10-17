"""Integration tests for POST /summarize endpoint."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_summarize_endpoint_success() -> None:
    """Tests POST /summarize endpoint with mocked LLM service."""
    payload = {
        "articles": [
            {
                "title": "Breakthrough in AI",
                "link": "https://example.com/ai-breakthrough",
                "content": "A new AI breakthrough occurred today.",
                "source": "AI Weekly",
            }
        ]
    }

    mock_markdown = (
        "## Top 5 Main Stories\n"
        "- **Breakthrough in AI**: A new breakthrough occurred.\n\n"
        "## Top 5 Quick Headlines\n"
        "- Quick headline note."
    )

    with patch("src.api.services.digest.generate_completion", return_value=mock_markdown):
        response = client.post("/api/summarize", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["article_count"] == 1
        assert data["digest"] == mock_markdown


def test_summarize_endpoint_empty_articles() -> None:
    """Tests POST /api/summarize endpoint when given an empty list of articles."""
    response = client.post("/api/summarize", json={"articles": []})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["article_count"] == 0
    assert "No articles provided" in data["digest"]
