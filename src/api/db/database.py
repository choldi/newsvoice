"""Database management and SQLite connection layer using aiosqlite."""

import json
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
    """Repository handling database operations for articles, clusters, and deliveries."""

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

    async def create_cluster(self, fingerprint: dict[str, str]) -> int:
        """Creates a new event cluster and returns its primary key ID.

        Args:
            fingerprint: Dictionary containing entity, action, and title hash.

        Returns:
            The newly inserted cluster ID.
        """
        now_iso = datetime.now(UTC).isoformat()
        fp_json = json.dumps(fingerprint, sort_keys=True)

        query = """
            INSERT INTO clusters (fingerprint, first_seen, last_seen)
            VALUES (?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (fp_json, now_iso, now_iso))
            await db.commit()
            return cursor.lastrowid or 0

    async def update_cluster_last_seen(self, cluster_id: int) -> None:
        """Updates the last_seen timestamp for an existing cluster.

        Args:
            cluster_id: ID of the cluster to update.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = "UPDATE clusters SET last_seen = ? WHERE id = ?"

        async with get_db(self.db_path) as db:
            await db.execute(query, (now_iso, cluster_id))
            await db.commit()

    async def add_article_to_cluster(self, cluster_id: int, article_url: str) -> None:
        """Associates an article URL with a cluster.

        Args:
            cluster_id: ID of the event cluster.
            article_url: URL of the article.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT OR IGNORE INTO cluster_articles (cluster_id, article_url, added_at)
            VALUES (?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.execute(query, (cluster_id, article_url, now_iso))
            await db.commit()

    async def find_recent_clusters(self, active_days: int = 3) -> list[dict]:
        """Retrieves recent active clusters within active_days window.

        Args:
            active_days: Time window in days to consider a cluster active.

        Returns:
            List of cluster dictionaries with id, fingerprint, and timestamps.
        """
        cutoff_date = (datetime.now(UTC) - timedelta(days=active_days)).isoformat()
        query = """
            SELECT id, fingerprint, first_seen, last_seen
            FROM clusters
            WHERE last_seen >= ?
            ORDER BY last_seen DESC
        """

        async with get_db(self.db_path) as db:
            async with db.execute(query, (cutoff_date,)) as cursor:
                rows = await cursor.fetchall()
                clusters = []
                for row in rows:
                    try:
                        fp = json.loads(row["fingerprint"])
                    except Exception:
                        fp = {}
                    clusters.append(
                        {
                            "id": row["id"],
                            "fingerprint": fp,
                            "first_seen": row["first_seen"],
                            "last_seen": row["last_seen"],
                        }
                    )
                return clusters

    async def is_cluster_delivered(self, cluster_id: int) -> bool:
        """Checks if a cluster has previously been delivered.

        Args:
            cluster_id: ID of the cluster.

        Returns:
            True if at least one delivery is recorded for this cluster ID.
        """
        query = "SELECT 1 FROM deliveries WHERE cluster_id = ? LIMIT 1"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (cluster_id,)) as cursor:
                row = await cursor.fetchone()
                return row is not None

    async def record_cluster_delivery(self, cluster_id: int, material_update: bool = False) -> None:
        """Records a delivery event for an event cluster.

        Args:
            cluster_id: ID of the delivered cluster.
            material_update: Whether this delivery represents a material update.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO deliveries (cluster_id, delivered_at, material_update)
            VALUES (?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (cluster_id, now_iso, 1 if material_update else 0))
            await db.commit()

    async def get_cluster_articles(self, cluster_id: int) -> list[dict[str, str | None]]:
        """Fetches all article details associated with a cluster.

        Args:
            cluster_id: ID of the cluster.

        Returns:
            List of article dictionaries.
        """
        query = """
            SELECT a.url, a.title, a.content, a.source, a.fetched_at
            FROM articles a
            JOIN cluster_articles ca ON a.url = ca.article_url
            WHERE ca.cluster_id = ?
            ORDER BY ca.added_at ASC
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (cluster_id,)) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "url": row["url"],
                        "title": row["title"],
                        "content": row["content"],
                        "source": row["source"],
                        "fetched_at": row["fetched_at"],
                    }
                    for row in rows
                ]
