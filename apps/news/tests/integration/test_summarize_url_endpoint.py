"""Integration tests for the Phase 7.10 POST /api/summarize/url endpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)
anon_client = TestClient(app)


@pytest.fixture(autouse=True)
def _tmp_collections_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.collections_file at an isolated TOML and enable auth."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    return tmp_path / "collections.toml"


def _write_collection(path: Path) -> None:
    """Write one collection named 'tech' into the isolated TOML file."""
    path.write_text(
        "[[collections]]\n"
        'name = "tech"\n'
        'time = "08:00"\n'
        'feed_urls = ["https://example.com/rss"]\n'
        'language = "es"\n'
        'delivery_channels = ["telegram", "email"]\n'
        'email_recipients = ["me@example.com"]\n'
    )


def test_url_summary_requires_auth() -> None:
    """Without the admin token the endpoint answers 401."""
    response = anon_client.post("/api/summarize/url", json={"url": "https://example.com/a"})
    assert response.status_code == 401


def test_url_summary_unknown_collection_404() -> None:
    """An unknown collection is rejected before any workflow starts."""
    response = client.post(
        "/api/summarize/url",
        json={"url": "https://example.com/a", "collection": "nope"},
    )
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/a", "javascript:alert(1)", "", "/relative"],
)
def test_url_summary_invalid_url_422(url: str) -> None:
    """Only well-formed http(s) URLs are accepted."""
    response = client.post("/api/summarize/url", json={"url": url})
    assert response.status_code == 422


def test_url_summary_starts_workflow_for_a_collection(_tmp_collections_file: Path) -> None:
    """The endpoint resolves the collection and starts the Temporal workflow."""
    _write_collection(_tmp_collections_file)

    with patch(
        "src.api.routes.summarize_url.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        return_value="url-summary-tech-20261003120000-abcd1234",
    ) as start:
        response = client.post(
            "/api/summarize/url",
            json={"url": "https://example.com/gpt5", "collection": "tech"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ok"
    assert body["collection"] == "tech"
    assert body["url"] == "https://example.com/gpt5"
    assert body["workflow_id"].startswith("url-summary-tech")

    kwargs = start.call_args.kwargs
    assert kwargs["url"] == "https://example.com/gpt5"
    assert kwargs["store"] is False
    assert kwargs["deliver"] is True
    assert start.call_args.args[0].name == "tech"


def test_url_summary_defaults_are_store_false_and_deliver_true(
    _tmp_collections_file: Path,
) -> None:
    """store defaults to false (nothing persisted) and delivery stays on."""
    _write_collection(_tmp_collections_file)

    with patch(
        "src.api.routes.summarize_url.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        return_value="url-summary-tech-1",
    ) as start:
        response = client.post(
            "/api/summarize/url",
            json={"url": "https://example.com/a", "collection": "tech"},
        )

    assert response.status_code == 202
    assert start.call_args.kwargs["store"] is False
    assert start.call_args.kwargs["deliver"] is True


def test_url_summary_store_flag_is_forwarded(_tmp_collections_file: Path) -> None:
    """store=true reaches the workflow configuration unchanged."""
    _write_collection(_tmp_collections_file)

    with patch(
        "src.api.routes.summarize_url.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        return_value="url-summary-tech-2",
    ) as start:
        response = client.post(
            "/api/summarize/url",
            json={"url": "https://example.com/a", "collection": "tech", "store": True},
        )

    assert response.status_code == 202
    assert start.call_args.kwargs["store"] is True


def test_url_summary_falls_back_to_the_default_collection(
    _tmp_collections_file: Path,
) -> None:
    """Without collections.toml the implicit default collection is used."""
    with patch(
        "src.api.routes.summarize_url.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        return_value="url-summary-default-1",
    ) as start:
        response = client.post("/api/summarize/url", json={"url": "https://example.com/a"})

    assert response.status_code == 202
    assert response.json()["collection"] == "default"
    assert start.call_args.args[0].name == "default"


def test_url_summary_temporal_failure_502(_tmp_collections_file: Path) -> None:
    """A Temporal connection problem surfaces as 502, not a stack trace."""
    _write_collection(_tmp_collections_file)

    with patch(
        "src.api.routes.summarize_url.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        side_effect=RuntimeError("temporal unreachable"),
    ):
        response = client.post(
            "/api/summarize/url",
            json={"url": "https://example.com/a", "collection": "tech"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Could not start the URL summary workflow"


def test_url_summary_disabled_403(
    _tmp_collections_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """url_summary_enabled=false closes the endpoint without a code change."""
    _write_collection(_tmp_collections_file)
    monkeypatch.setattr(settings, "url_summary_enabled", False)

    response = client.post("/api/summarize/url", json={"url": "https://example.com/a"})

    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]
