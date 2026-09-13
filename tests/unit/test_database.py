"""Unit tests for SQLite database layer and migrations."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.api.db.database import ArticleRepository, get_db, run_migrations


@pytest.mark.asyncio
async def test_run_migrations(tmp_path: Path) -> None:
    """Tests executing SQL migrations on a clean temporary database."""
    db_file = tmp_path / "test_news.db"
    await run_migrations(str(db_file))

    async with get_db(str(db_file)) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}

        assert "articles" in tables
        assert "deliveries" in tables
        assert "schema_migrations" in tables
        assert "digest_runs" in tables


@pytest.mark.asyncio
async def test_article_repository_save_and_get_seen(tmp_path: Path) -> None:
    """Tests saving articles and querying seen URLs within retention window."""
    db_file = tmp_path / "test_news.db"
    await run_migrations(str(db_file))
    repo = ArticleRepository(str(db_file))

    articles_to_save = [
        {
            "url": "https://example.com/art1",
            "title": "Article 1",
            "content": "Content 1",
            "source": "Source 1",
        }
    ]
    await repo.save_articles(articles_to_save)

    seen = await repo.get_seen_urls(
        ["https://example.com/art1", "https://example.com/art2"], days_window=7
    )
    assert "https://example.com/art1" in seen
    assert "https://example.com/art2" not in seen


@pytest.mark.asyncio
async def test_article_repository_records_deliveries(tmp_path: Path) -> None:
    """Tests recording delivery history in the deliveries table."""
    db_file = tmp_path / "test_news.db"
    await run_migrations(str(db_file))
    repo = ArticleRepository(str(db_file))

    await repo.save_articles(
        [
            {
                "url": "https://example.com/delivered",
                "title": "Delivered Article",
                "content": "Delivered Content",
                "source": "BBC",
            }
        ]
    )

    await repo.record_deliveries(["https://example.com/delivered"])

    async with get_db(str(db_file)) as db:
        async with db.execute("SELECT * FROM deliveries") as cursor:
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["article_url"] == "https://example.com/delivered"


@pytest.mark.asyncio
async def test_article_repository_window_expiration(tmp_path: Path) -> None:
    """Tests that articles older than days_window are not returned as seen."""
    db_file = tmp_path / "test_news.db"
    await run_migrations(str(db_file))
    repo = ArticleRepository(str(db_file))

    # Insert an article with timestamp from 10 days ago
    old_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    async with get_db(str(db_file)) as db:
        await db.execute(
            """
            INSERT INTO articles (url, title, content, source, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("https://example.com/old", "Old News", "Old Content", "Old Source", old_time),
        )
        await db.commit()

    seen_7_days = await repo.get_seen_urls(["https://example.com/old"], days_window=7)
    assert "https://example.com/old" not in seen_7_days

    seen_14_days = await repo.get_seen_urls(["https://example.com/old"], days_window=14)
    assert "https://example.com/old" in seen_14_days


@pytest.mark.asyncio
async def test_article_repository_collection_scoped_dedup(tmp_path: Path) -> None:
    """Articles are deduplicated per collection, not globally."""
    db_file = tmp_path / "test_news.db"
    await run_migrations(str(db_file))
    repo = ArticleRepository(str(db_file))

    article = {
        "url": "https://example.com/shared",
        "title": "Shared Story",
        "content": "Content",
        "source": "RSS",
    }
    await repo.save_articles([article], collection="tech")

    # Seen again within the window in the same collection.
    seen_tech = await repo.get_seen_urls(
        ["https://example.com/shared"], days_window=7, collection="tech"
    )
    assert "https://example.com/shared" in seen_tech

    # Not considered seen in a collection that never stored it.
    seen_other = await repo.get_seen_urls(
        ["https://example.com/shared"], days_window=7, collection="finance"
    )
    assert "https://example.com/shared" not in seen_other

    # Untouched "default" collection is unaffected either.
    seen_default = await repo.get_seen_urls(
        ["https://example.com/shared"], days_window=7, collection="default"
    )
    assert "https://example.com/shared" not in seen_default
