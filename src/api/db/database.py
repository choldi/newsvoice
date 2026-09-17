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
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")
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
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
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

    async def get_seen_urls(
        self,
        urls: list[str],
        days_window: int = 7,
        collection: str = "default",
    ) -> set[str]:
        """Returns the subset of URLs seen in a collection within days_window.

        Args:
            urls: List of normalized URLs to check.
            days_window: Retention window in days to consider an article recently seen.
            collection: Collection name limiting the dedup scope.

        Returns:
            Set of URLs that are already stored in the database for the collection.
        """
        if not urls:
            return set()

        cutoff_date = (datetime.now(UTC) - timedelta(days=days_window)).isoformat()
        placeholders = ",".join("?" for _ in urls)
        query = f"""
            SELECT url FROM articles
            WHERE collection = ?
            AND url IN ({placeholders})
            AND fetched_at >= ?
        """
        params = [collection] + list(urls) + [cutoff_date]

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return {row["url"] for row in rows}

    async def save_articles(
        self,
        articles_data: list[dict[str, str | None]],
        collection: str = "default",
    ) -> None:
        """Persists new articles into the articles table for a collection.

        Args:
            articles_data: List of article dicts with keys 'url', 'title', 'content', 'source'.
            collection: Collection name the articles belong to.
        """
        if not articles_data:
            return

        now_iso = datetime.now(UTC).isoformat()
        records = [
            (
                collection,
                item["url"],
                item["title"],
                item.get("content"),
                item.get("source"),
                now_iso,
            )
            for item in articles_data
        ]

        query = """
            INSERT OR REPLACE INTO articles (collection, url, title, content, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.executemany(query, records)
            await db.commit()

    async def record_deliveries(
        self,
        article_urls: list[str],
        collection: str = "default",
    ) -> None:
        """Records delivery timestamps for delivered article URLs in a collection.

        Args:
            article_urls: List of article URLs that were included in a delivery digest.
            collection: Collection name the delivery belongs to.
        """
        if not article_urls:
            return

        now_iso = datetime.now(UTC).isoformat()
        records = [(collection, url, now_iso) for url in article_urls]

        query = """
            INSERT INTO deliveries (collection, article_url, delivered_at)
            VALUES (?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.executemany(query, records)
            await db.commit()

    async def create_cluster(
        self,
        fingerprint: dict[str, str],
        collection: str = "default",
    ) -> int:
        """Creates a new event cluster for a collection and returns its primary key ID.

        Args:
            fingerprint: Dictionary containing entity, action, and title hash.
            collection: Collection name the cluster belongs to.

        Returns:
            The newly inserted cluster ID.
        """
        now_iso = datetime.now(UTC).isoformat()
        fp_json = json.dumps(fingerprint, sort_keys=True)

        query = """
            INSERT INTO clusters (collection, fingerprint, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (collection, fp_json, now_iso, now_iso))
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

    async def add_article_to_cluster(
        self,
        cluster_id: int,
        article_url: str,
        collection: str = "default",
    ) -> None:
        """Associates an article URL with a cluster in a collection.

        Args:
            cluster_id: ID of the event cluster.
            article_url: URL of the article.
            collection: Collection name for the association.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT OR IGNORE INTO cluster_articles (collection, cluster_id, article_url, added_at)
            VALUES (?, ?, ?, ?)
        """

        async with get_db(self.db_path) as db:
            await db.execute(query, (collection, cluster_id, article_url, now_iso))
            await db.commit()

    async def find_recent_clusters(
        self,
        active_days: int = 3,
        collection: str = "default",
    ) -> list[dict]:
        """Retrieves recent active clusters within active_days window for a collection.

        Args:
            active_days: Time window in days to consider a cluster active.
            collection: Collection name to limit cluster lookup to.

        Returns:
            List of cluster dictionaries with id, fingerprint, and timestamps.
        """
        cutoff_date = (datetime.now(UTC) - timedelta(days=active_days)).isoformat()
        query = """
            SELECT id, fingerprint, first_seen, last_seen
            FROM clusters
            WHERE collection = ?
            AND last_seen >= ?
            ORDER BY last_seen DESC
        """

        async with get_db(self.db_path) as db:
            async with db.execute(query, (collection, cutoff_date)) as cursor:
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

    async def record_cluster_delivery(
        self,
        cluster_id: int,
        material_update: bool = False,
        collection: str = "default",
    ) -> None:
        """Records a delivery event for an event cluster in a collection.

        Args:
            cluster_id: ID of the delivered cluster.
            material_update: Whether this delivery represents a material update.
            collection: Collection name the delivery belongs to.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO deliveries (collection, cluster_id, delivered_at, material_update)
            VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (collection, cluster_id, now_iso, 1 if material_update else 0))
            await db.commit()

    async def get_cluster_articles(
        self,
        cluster_id: int,
        collection: str = "default",
    ) -> list[dict[str, str | None]]:
        """Fetches all article details associated with a cluster in a collection.

        Args:
            cluster_id: ID of the cluster.
            collection: Collection name the cluster belongs to.

        Returns:
            List of article dictionaries.
        """
        query = """
            SELECT a.url, a.title, a.content, a.source, a.fetched_at
            FROM articles a
            JOIN cluster_articles ca
              ON a.collection = ca.collection AND a.url = ca.article_url
            WHERE ca.cluster_id = ? AND ca.collection = ?
            ORDER BY ca.added_at ASC
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (cluster_id, collection)) as cursor:
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


class RunsRepository:
    """Repository for digest run history consumed by the admin GUI."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def record_run(
        self,
        workflow_id: str,
        collection: str,
        status: str,
        article_count: int,
        cluster_count: int,
        digest_preview: str | None = None,
        audio_path: str | None = None,
        error: str | None = None,
    ) -> int:
        """Upsert a digest pipeline run row keyed by workflow_id.

        Args:
            workflow_id: Temporal workflow id of the run.
            collection: Collection name the run belongs to.
            status: Run outcome ('ok', 'no_new_articles', 'failed').
            article_count: Number of articles summarized (0 for no-new-articles).
            cluster_count: Number of clusters summarized.
            digest_preview: First chars of the generated digest.
            audio_path: Generated audio file path, if any.
            error: Error message for failed runs.

        Returns:
            The row id of the recorded run.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO digest_runs (
                workflow_id, collection, status, article_count, cluster_count,
                digest_preview, audio_path, error, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                collection = excluded.collection,
                status = excluded.status,
                article_count = excluded.article_count,
                cluster_count = excluded.cluster_count,
                digest_preview = excluded.digest_preview,
                audio_path = excluded.audio_path,
                error = excluded.error,
                completed_at = excluded.completed_at
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (
                    workflow_id,
                    collection,
                    status,
                    int(article_count or 0),
                    int(cluster_count or 0),
                    digest_preview,
                    audio_path,
                    error,
                    now_iso,
                    now_iso,
                ),
            )
            await db.commit()
            async with db.execute(
                "SELECT id FROM digest_runs WHERE workflow_id = ?", (workflow_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return int(row["id"]) if row else 0

    async def list_runs(
        self,
        collection: str | None = None,
        status_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List recent digest runs, newest first, with optional filters.

        Args:
            collection: Filter by collection name.
            status_filter: Filter by run status.
            limit: Maximum number of rows to return.
            offset: Pagination offset.

        Returns:
            List of run dicts.
        """
        conditions: list[str] = []
        params: list[object] = []
        if collection:
            conditions.append("collection = ?")
            params.append(collection)
        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT id, workflow_id, collection, status, article_count, cluster_count,
                   digest_preview, audio_path, error, started_at, completed_at
            FROM digest_runs
            {where}
            ORDER BY completed_at DESC
            LIMIT ? OFFSET ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, [*params, limit, offset]) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_run(self, run_id: int) -> dict | None:
        """Return a single run by its database id.

        Args:
            run_id: Id of the digest run row.

        Returns:
            The run dict, or None if not found.
        """
        query = """
            SELECT id, workflow_id, collection, status, article_count, cluster_count,
                   digest_preview, audio_path, error, started_at, completed_at
            FROM digest_runs
            WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def count(self, collection: str | None = None, status_filter: str | None = None) -> int:
        """Count runs matching the optional filters.

        Args:
            collection: Filter by collection name.
            status_filter: Filter by run status.

        Returns:
            Matching row count.
        """
        conditions: list[str] = []
        params: list[object] = []
        if collection:
            conditions.append("collection = ?")
            params.append(collection)
        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with get_db(self.db_path) as db:
            async with db.execute(f"SELECT COUNT(*) FROM digest_runs {where}", params) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0


class AudioJobsRepository:
    """Repository for per-run TTS generation status (STARTING/GENERATING/FINISHED)."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def start_job(self, run_id: str, collection: str = "default") -> None:
        """Insert or reset the audio job row for a run to STARTING.

        Idempotent (upsert on run_id) so Temporal replays never create dupes.

        Args:
            run_id: Workflow id of the digest run the job belongs to.
            collection: Collection name the run belongs to.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO audio_jobs (run_id, collection, status, started_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                collection = excluded.collection,
                status = 'STARTING',
                error = NULL,
                audio_path = NULL,
                started_at = excluded.started_at,
                finished_at = NULL,
                elapsed_seconds = NULL
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (run_id, collection, "STARTING", now_iso))
            await db.commit()

    async def set_generating(self, run_id: str) -> None:
        """Mark the job as GENERATING (synthesis in progress).

        Args:
            run_id: Workflow id of the digest run.
        """
        query = "UPDATE audio_jobs SET status = ? WHERE run_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, ("GENERATING", run_id))
            await db.commit()

    async def finish_job(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        audio_path: str | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        """Record the terminal TTS outcome for a run.

        Args:
            run_id: Workflow id of the digest run.
            status: 'FINISHED_OK' or 'FINISHED_ERROR'.
            error: Error message/observation for failed generation.
            audio_path: Generated audio file path on success.
            elapsed_seconds: Wall-clock seconds the TTS step took.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            UPDATE audio_jobs
            SET status = ?, error = ?, audio_path = ?,
                finished_at = ?, elapsed_seconds = ?
            WHERE run_id = ?
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (status, error, audio_path, now_iso, elapsed_seconds, run_id),
            )
            await db.commit()

    async def get_job(self, run_id: str) -> dict | None:
        """Return the audio job row for a run, or None if not found.

        Args:
            run_id: Workflow id of the digest run.

        Returns:
            The audio job dict, or None.
        """
        query = """
            SELECT id, run_id, collection, status, error, audio_path,
                   started_at, finished_at, elapsed_seconds
            FROM audio_jobs
            WHERE run_id = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
