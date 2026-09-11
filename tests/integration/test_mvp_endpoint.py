"""Integration tests for POST /mvp_digest endpoint and /health."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    """Tests /health GET endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == "ok"
    assert json_data["service"] == "news-engine-api"


def test_mvp_digest_endpoint_success() -> None:
    """Tests POST /mvp_digest endpoint with mocked LLM service."""
    payload = {
        "articles": [
            {
                "title": "Major Discovery",
                "link": "https://example.com/news/1",
                "content": "Scientists made a major breakthrough today.",
                "source": "Science Daily",
            }
        ]
    }

    mock_markdown = "# Daily News\n\n- Major Discovery"

    with patch("src.api.services.digest.generate_completion", return_value=mock_markdown):
        response = client.post("/api/mvp_digest", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert data["article_count"] == 1
        assert data["digest"] == mock_markdown


def test_mvp_digest_endpoint_invalid_payload() -> None:
    """Tests POST /api/mvp_digest endpoint error handling with invalid schema."""
    response = client.post("/api/mvp_digest", json={"articles": "not-a-list"})
    assert response.status_code == 422  # Unprocessable Entity
