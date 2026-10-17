"""Integration tests for POST /filter_new endpoint."""

from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_filter_new_first_and_second_run_deduplication() -> None:
    """Tests that first run returns new articles, and immediate rerun returns zero duplicates."""
    payload = {
        "articles": [
            {
                "title": "Article One",
                "link": "https://example.com/art1?utm_source=twitter",
                "content": "Content of article one",
                "source": "TechCrunch",
            },
            {
                "title": "Article Two",
                "link": "https://example.com/art2",
                "content": "Content of article two",
                "source": "TechCrunch",
            },
        ]
    }

    # First run: should return 2 new articles with clean URLs
    response1 = client.post("/api/filter_new", json=payload)
    assert response1.status_code == 200
    data1 = response1.json()
    assert data1["status"] == "ok"
    assert data1["total_received"] == 2
    assert data1["new_count"] == 2
    assert data1["new_articles"][0]["link"] == "https://example.com/art1"

    # Second run with same articles: should return 0 new articles
    response2 = client.post("/api/filter_new", json=payload)
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["status"] == "ok"
    assert data2["total_received"] == 2
    assert data2["new_count"] == 0
    assert len(data2["new_articles"]) == 0


def test_filter_new_empty_payload() -> None:
    """Tests POST /api/filter_new with empty articles array."""
    response = client.post("/api/filter_new", json={"articles": []})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["new_count"] == 0
    assert data["new_articles"] == []
