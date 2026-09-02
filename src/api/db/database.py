"""Database management and SQLite connection layer using aiosqlite."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import structlog

from src.core.config import settings

logger = structlog.get_logger()


def get_migrations_dir() -> Path:
    """Returns the path to the database migrations directory.

    Returns:
        Path object pointing to the migrations folder.
    """
    return Path(__file__).parent / "migrations"


async def run_migrations(db_path: str | None = None) -> None:
    """Applies all pending SQL migrations to the SQLite database.

    Args:
        db_path: Optional path to the database file. Defaults to settings.database_path.
    """
    target_path = db_path or settings.database_path
    db_file = Path(target_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    migrations_dir = get_migrations_dir()
    if not migrations_dir.exists():
        logger.warning("migrations_dir_not_found", path=str(migrations_dir))
        return

    migration_files = sorted(migrations_dir.glob("*.sql"))

    async with aiosqlite.connect(target_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.commit()

        async with db.execute("SELECT version FROM schema_migrations") as cursor:
            applied_rows = await cursor.fetchall()
            applied_versions = {row[0] for row in applied_rows}

        for file_path in migration_files:
            version = file_path.name
            if version not in applied_versions:
                logger.info("applying_migration", version=version)
                sql_content = file_path.read_text(encoding="utf-8")
                await db.executescript(sql_content)
                await db.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
                await db.commit()
                logger.info("migration_applied_successfully", version=version)


@asynccontextmanager
async def get_db(db_path: str | None = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an active aiosqlite database connection.

    Args:
        db_path: Optional path to SQLite database. Defaults to settings.database_path.

    Yields:
        An open aiosqlite.Connection instance.
    """
    target_path = db_path or settings.database_path
    db_file = Path(target_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(target_path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


class ArticleRepository:
    """Repository handling database operations for articles and deliveries."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initializes the repository with an optional database path.

        Args:
            db_path: Path to the SQLite database.
        """
        self.db_path = db_path

    async def get_seen_urls(self, urls: list[str], days_window: int = 7) -> set[str]:
        """Returns the subset of URLs that have already been recorded within days_window.

        Args:
            urls: List of normalized URLs to check.
            days_window: Retention window in days to consider an article recently seen.

        Returns:
            Set of URLs that are already stored in the database.
        """
        if not urls:
            return set()

        cutoff_date = (datetime.now(UTC) - timedelta(days=days_window)).isoformat()
        placeholders = ",".join("?" for _ in urls)
        query = f"""
            SELECT url FROM articles
            WHERE url IN ({placeholders})
            AND fetched_at >= ?
        """
        params = list(urls) + [cutoff_date]

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return {row["url"] for row in rows}

    async def save_articles(
        self,
        articles_data: list[dict[str, str | None]],
    ) -> None:
        """Persists new articles into the articles table.

        Args:
            articles_data: List of article dicts with keys 'url', 'title', 'content', 'source'.
        """
        if not articles_data:
            return

        now_iso = datetime.now(UTC).isoformat()
        records = [
            (
                item["url"],
                item["title"],
                item.get("content"),
                item.get("source"),
                now_iso,
            )
            for item in articles_data
        ]

        query = """
            INSERT OR REPLACE INTO articles (url, title, content, source, fetched_at)
            VALUES (?, ?, ?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.executemany(query, records)
            await db.commit()

    async def record_deliveries(self, article_urls: list[str]) -> None:
        """Records delivery timestamps for a list of delivered article URLs.

        Args:
            article_urls: List of article URLs that were included in a delivery digest.
        """
        if not article_urls:
            return

        now_iso = datetime.now(UTC).isoformat()
        records = [(url, now_iso) for url in article_urls]

        query = """
            INSERT INTO deliveries (article_url, delivered_at)
            VALUES (?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.executemany(query, records)
            await db.commit()
