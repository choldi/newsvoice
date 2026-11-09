"""Database management and SQLite connection layer using aiosqlite."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

try:  # pragma: no cover - fcntl is unavailable on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from src.api.models.narration import NarrationScript
from src.core.config import settings

logger = structlog.get_logger()


def _acquire_migration_lock(target_path: str):
    """Serialises migrations across processes sharing the same SQLite file."""
    lock_path = Path(f"{target_path}.migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_migration_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


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

    lock_handle = await asyncio.to_thread(_acquire_migration_lock, target_path)
    try:
        async with aiosqlite.connect(target_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=30000")
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
                        "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
                        (version,),
                    )
                    await db.commit()
                    logger.info("migration_applied_successfully", version=version)
    finally:
        await asyncio.to_thread(_release_migration_lock, lock_handle)


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
        narration_path: str | None = None,
    ) -> int:
        """Upsert a digest pipeline run row keyed by workflow_id.

        Args:
            workflow_id: Temporal workflow id of the run.
            collection: Collection name the run belongs to.
            status: Run outcome ('ok', 'no_new_articles', 'failed').
            article_count: Number of articles summarized (0 for no-new-articles).
            cluster_count: Number of clusters summarized.
            digest_preview: First chars of the generated digest.
            audio_path: Generated digest TTS audio file path, if any.
            error: Error message for failed runs.
            narration_path: Generated narration audio file path, if any. Kept
                when omitted, so the digest workflow does not erase a path
                already written by the narration worker.

        Returns:
            The row id of the recorded run.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO digest_runs (
                workflow_id, collection, status, article_count, cluster_count,
                digest_preview, audio_path, narration_path, error, started_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                collection = excluded.collection,
                status = excluded.status,
                article_count = excluded.article_count,
                cluster_count = excluded.cluster_count,
                digest_preview = excluded.digest_preview,
                audio_path = excluded.audio_path,
                narration_path = COALESCE(
                    excluded.narration_path, digest_runs.narration_path
                ),
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
                    narration_path,
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
                   digest_preview, audio_path, narration_path, error, started_at,
                   completed_at
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
                   digest_preview, audio_path, narration_path, error, started_at,
                   completed_at
            FROM digest_runs
            WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_run_by_workflow_id(self, workflow_id: str) -> dict | None:
        """Return a single run by its Temporal workflow id.

        Args:
            workflow_id: Workflow id of the digest run.

        Returns:
            The run dict, or None if not found.
        """
        query = """
            SELECT id, workflow_id, collection, status, article_count, cluster_count,
                   digest_preview, audio_path, narration_path, error, started_at,
                   completed_at
            FROM digest_runs
            WHERE workflow_id = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (workflow_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def set_narration_path(self, workflow_id: str, narration_path: str | None) -> int:
        """Attach or clear a generated narration file on an existing run.

        The narration worker runs as a separate workflow, so it must not rewrite
        the whole run row (which would reset the digest counters and status);
        only the narration column is touched. Phase 9.8: a ``None`` value clears
        the column (the tools page uses it when deleting a generated audio).

        Args:
            workflow_id: Temporal workflow id of the run that was narrated.
            narration_path: Path of the generated narration audio file, or
                None to clear it.

        Returns:
            Number of rows updated (0 when no run matches the workflow id).
        """
        query = "UPDATE digest_runs SET narration_path = ? WHERE workflow_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (narration_path, workflow_id))
            await db.commit()
            async with db.execute("SELECT changes()") as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0

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
    """Repository for per-run TTS generation status (STARTING/GENERATING/FINISHED).

    Rows are keyed by (run_id, kind): 'tts' is the digest single-voice job and
    'narration' the Phase 7 multi-speaker job, which must not overwrite it.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def start_job(self, run_id: str, collection: str = "default", kind: str = "tts") -> None:
        """Insert or reset the audio job row for a run to STARTING.

        Idempotent (upsert on run_id + kind) so Temporal replays never create dupes.

        Args:
            run_id: Workflow id of the digest run the job belongs to.
            collection: Collection name the run belongs to.
            kind: Audio job kind ('tts' or 'narration').
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO audio_jobs (run_id, collection, kind, status, started_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, kind) DO UPDATE SET
                collection = excluded.collection,
                status = 'STARTING',
                error = NULL,
                audio_path = NULL,
                started_at = excluded.started_at,
                finished_at = NULL,
                elapsed_seconds = NULL,
                paused = 0,
                pause_reason = NULL,
                chapter_start = NULL,
                chapter_end = NULL,
                partial_path = NULL
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (run_id, collection, kind, "STARTING", now_iso))
            await db.commit()

    async def set_generating(self, run_id: str, kind: str = "tts") -> None:
        """Mark the job as GENERATING (synthesis in progress).

        Args:
            run_id: Workflow id of the digest run.
            kind: Audio job kind ('tts' or 'narration').
        """
        query = "UPDATE audio_jobs SET status = ? WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, ("GENERATING", run_id, kind))
            await db.commit()

    async def set_status(self, run_id: str, status: str, kind: str = "tts") -> None:
        """Move a job to an arbitrary non-terminal status (Phase 8.4).

        Used for the PAUSED state a long document render enters while a
        scheduled digest owns the engine: the GUI must be able to tell
        "stalled" from "deliberately waiting", and only the activity in the
        middle of the synthesis knows which one it is.

        Args:
            run_id: Workflow id of the run the job belongs to.
            status: New status ('GENERATING', 'PAUSED', ...).
            kind: Audio job kind ('tts', 'narration', 'doc_tts', ...).
        """
        query = "UPDATE audio_jobs SET status = ? WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (status, run_id, kind))
            await db.commit()

    async def set_pause_state(self, run_id: str, reason: str | None, kind: str = "tts") -> None:
        """Mark a job paused for a concrete reason, or resume it.

        ``audio_jobs.status`` alone cannot tell an operator pause from a render
        that yielded the engine to a scheduled digest. The render gate reports
        the transition with its cause, and this method persists both together so
        the API and UI never observe a half-updated pair.

        Args:
            run_id: Workflow id of the job.
            reason: 'operator' or 'lease' to pause; None or '' to resume.
            kind: Audio job kind ('doc_tts', 'audiobook', ...).
        """
        if reason:
            query = (
                "UPDATE audio_jobs SET status = 'PAUSED', pause_reason = ? "
                "WHERE run_id = ? AND kind = ?"
            )
            params: tuple[object, ...] = (reason, run_id, kind)
        else:
            query = (
                "UPDATE audio_jobs SET status = 'GENERATING', pause_reason = NULL "
                "WHERE run_id = ? AND kind = ?"
            )
            params = (run_id, kind)
        async with get_db(self.db_path) as db:
            await db.execute(query, params)
            await db.commit()

    async def set_paused(self, run_id: str, paused: bool, kind: str = "tts") -> bool:
        """Set or clear the operator pause flag of a running job.

        This is the flag the render gate polls between chunks. It is stored in
        the database (not the job metadata sidecar) so the API and the worker
        never race on a read-modify-write JSON file.

        Args:
            run_id: Workflow id of the job.
            paused: True to pause after the current chunk, False to resume.
            kind: Audio job kind ('audiobook', ...).

        Returns:
            True when a row was updated, False when the job does not exist.
        """
        query = "UPDATE audio_jobs SET paused = ? WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (1 if paused else 0, run_id, kind))
            await db.commit()
        return bool(cursor.rowcount)

    async def is_paused(self, run_id: str, kind: str = "tts") -> bool:
        """Return whether the operator paused this job.

        Args:
            run_id: Workflow id of the job.
            kind: Audio job kind ('audiobook', ...).

        Returns:
            True when the pause flag is set; False for a missing row too, so a
            deleted job never wedges a render forever.
        """
        query = "SELECT paused FROM audio_jobs WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id, kind)) as cursor:
                row = await cursor.fetchone()
        return bool(row["paused"]) if row is not None else False

    async def set_chapter_range(
        self,
        run_id: str,
        chapter_start: int | None,
        chapter_end: int | None,
        kind: str = "tts",
    ) -> bool:
        """Record the chapter slice a render is actually producing.

        The activity writes the clamped range after extraction, when the real
        chapter count is known; the submit endpoint stores the requested one.

        Args:
            run_id: Workflow id of the job.
            chapter_start: First chapter (1-based) or None for the whole book.
            chapter_end: Last chapter (inclusive) or None for the last one.
            kind: Audio job kind ('audiobook', ...).

        Returns:
            True when a row was updated, False when the job does not exist.
        """
        query = (
            "UPDATE audio_jobs SET chapter_start = ?, chapter_end = ? WHERE run_id = ? AND kind = ?"
        )
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (chapter_start, chapter_end, run_id, kind))
            await db.commit()
        return bool(cursor.rowcount)

    async def set_partial_path(
        self, run_id: str, partial_path: str | None, kind: str = "tts"
    ) -> bool:
        """Record where the latest partial M4B of a job was written.

        Args:
            run_id: Workflow id of the job.
            partial_path: Path of the partial file, or None to forget it.
            kind: Audio job kind ('audiobook', ...).

        Returns:
            True when a row was updated, False when the job does not exist.
        """
        query = "UPDATE audio_jobs SET partial_path = ? WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (partial_path, run_id, kind))
            await db.commit()
        return bool(cursor.rowcount)

    async def finish_job(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        audio_path: str | None = None,
        elapsed_seconds: float | None = None,
        kind: str = "tts",
    ) -> None:
        """Record the terminal TTS outcome for a run.

        Args:
            run_id: Workflow id of the digest run.
            status: 'FINISHED_OK' or 'FINISHED_ERROR'.
            error: Error message/observation for failed generation.
            audio_path: Generated audio file path on success.
            elapsed_seconds: Wall-clock seconds the TTS step took.
            kind: Audio job kind ('tts' or 'narration').
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            UPDATE audio_jobs
            SET status = ?, error = ?, audio_path = ?,
                finished_at = ?, elapsed_seconds = ?, paused = 0,
                pause_reason = NULL
            WHERE run_id = ? AND kind = ?
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (status, error, audio_path, now_iso, elapsed_seconds, run_id, kind),
            )
            await db.commit()

    async def get_job(self, run_id: str, kind: str = "tts") -> dict | None:
        """Return the audio job row for a run, or None if not found.

        Args:
            run_id: Workflow id of the digest run.
            kind: Audio job kind ('tts' or 'narration').

        Returns:
            The audio job dict, or None.
        """
        query = """
            SELECT id, run_id, collection, kind, status, error, audio_path,
                   started_at, finished_at, elapsed_seconds,
                   paused, pause_reason, chapter_start, chapter_end, partial_path
            FROM audio_jobs
            WHERE run_id = ? AND kind = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id, kind)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_jobs(self, collection: str | None = None, kind: str | None = None) -> list[dict]:
        """List audio jobs filtered by collection and/or kind, newest first.

        Args:
            collection: Optional collection filter.
            kind: Optional audio job kind filter.

        Returns:
            List of audio job dicts.
        """
        conditions: list[str] = []
        params: list[object] = []
        if collection:
            conditions.append("collection = ?")
            params.append(collection)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT id, run_id, collection, kind, status, error, audio_path,
                   started_at, finished_at, elapsed_seconds,
                   paused, pause_reason, chapter_start, chapter_end, partial_path
            FROM audio_jobs
            {where}
            ORDER BY started_at DESC
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def delete_job(self, run_id: str, kind: str) -> dict | None:
        """Delete an audio job row and return what it had generated.

        Phase 9.8: the tools page offers delete per generated audio; the row
        tells the caller which files the workflow produced (audio_path and the
        sidecar it implies) so the service can remove them from disk.

        Args:
            run_id: Workflow id of the run the job belongs to.
            kind: Audio job kind ('doc_tts', 'lesson', 'narration').

        Returns:
            The deleted job dict (with its ``audio_path``), or None when no
            row matched.
        """
        job = await self.get_job(run_id, kind)
        if job is None:
            return None
        query = "DELETE FROM audio_jobs WHERE run_id = ? AND kind = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (run_id, kind))
            await db.commit()
        return job


class NarrationRepository:
    """Repository for narration scripts (Phase 7).

    Stores the authored script that the narration renderer turns into audio.
    Scripts are kept as JSON so the renderer can evolve without migrations.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def save_script(
        self,
        run_id: str,
        script: NarrationScript,
        collection: str = "default",
    ) -> int:
        """Insert a narration script row for a run.

        Args:
            run_id: Workflow id (or narration id) of the run the script belongs to.
            script: Validated script model to persist.
            collection: Collection name the run belongs to.

        Returns:
            The row id of the saved script.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO narration_scripts
                (run_id, collection, source, language, title, script_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (
                    run_id,
                    collection,
                    script.source,
                    script.language,
                    script.title,
                    script.model_dump_json(),
                    now_iso,
                ),
            )
            await db.commit()
            async with db.execute(
                "SELECT id FROM narration_scripts WHERE id = last_insert_rowid()"
            ) as cursor:
                row = await cursor.fetchone()
                return int(row["id"]) if row else 0

    async def update_script_json(self, script_id: int, script: NarrationScript) -> bool:
        """Rewrite the stored JSON of an existing narration script.

        Used by the resolver after respelling a script: ``text`` stays the
        human-readable version and ``tts_text`` is persisted next to it, so the
        same run renders with the same pronunciation without another LLM call.

        Args:
            script_id: Id of the narration_scripts row.
            script: Enriched script to store.

        Returns:
            True when a row was updated, False when the id does not exist.
        """
        query = """
            UPDATE narration_scripts
            SET script_json = ?, language = ?, title = ?
            WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                query,
                (script.model_dump_json(), script.language, script.title, script_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_script(self, script_id: int) -> NarrationScript | None:
        """Return a narration script by its database id.

        Args:
            script_id: Id of the narration_scripts row.

        Returns:
            The parsed NarrationScript, or None if not found.
        """
        query = """
            SELECT id, run_id, collection, source, language, title, script_json, created_at
            FROM narration_scripts
            WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (script_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return NarrationScript.model_validate_json(row["script_json"])

    async def latest_script_for_run(self, run_id: str) -> NarrationScript | None:
        """Return the most recent script saved for a run.

        Args:
            run_id: Workflow id of the run.

        Returns:
            The parsed NarrationScript, or None if the run has no script.
        """
        query = """
            SELECT id, run_id, collection, source, language, title, script_json, created_at
            FROM narration_scripts
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT 1
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return NarrationScript.model_validate_json(row["script_json"])


class SyncStateRepository:
    """Small KV store used by the collections-config monitor."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def get(self, key: str) -> str | None:
        """Return the stored value for a key, or None.

        Args:
            key: State key to look up (e.g. 'collections_signature').

        Returns:
            The stored value, or None if the key is absent.
        """
        query = "SELECT value FROM kv_state WHERE key = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (key,)) as cursor:
                row = await cursor.fetchone()
                return str(row[0]) if row else None

    async def set(self, key: str, value: str) -> None:
        """Upsert a state value, replacing any previous value for the key.

        Args:
            key: State key to write.
            value: Value to store for the key.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO kv_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (key, value, now_iso))
            await db.commit()


class MonitorRepository:
    """Repository for the system-monitor event log (SQLite retention)."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def record_event(self, check_name: str, status: str, detail: str | None) -> None:
        """Persist one check outcome.

        Args:
            check_name: Name of the check (e.g. 'edge_tts').
            status: Outcome ('ok', 'warning', 'error').
            detail: Optional human-readable detail.
        """
        now_iso = datetime.now(UTC).isoformat()
        query = """
            INSERT INTO monitor_events (ts, check_name, status, detail)
            VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (now_iso, check_name, status, detail))
            await db.commit()

    async def list_recent(self, limit: int = 100) -> list[dict]:
        """Return the latest monitor events, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            List of event dicts with ts, check_name, status, detail.
        """
        query = """
            SELECT id, ts, check_name, status, detail
            FROM monitor_events
            ORDER BY ts DESC
            LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def purge_before(self, retention_days: int) -> int:
        """Delete monitor events older than retention_days.

        Args:
            retention_days: Keep events newer than this many days.

        Returns:
            Number of deleted rows.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        query = "DELETE FROM monitor_events WHERE ts < ?"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (cutoff,))
            await db.commit()
            return cursor.rowcount

    async def count(self) -> int:
        """Return the total number of stored monitor events.

        Returns:
            Row count of the monitor_events table.
        """
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM monitor_events") as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0


class ModalSpendRepository:
    """Repository for the Modal GPU spend ledger (Phase 7.8)."""

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def record(
        self,
        *,
        collection: str,
        stage: str,
        backend: str,
        gpu_seconds: float,
        cost_usd: float,
        run_id: str | None = None,
        audio_seconds: float | None = None,
        cache_hit: bool = False,
        wall_seconds: float | None = None,
        cost_source: str = "estimate",
        gpu_type: str | None = None,
        billed_delta_usd: float | None = None,
    ) -> None:
        """Persist one Modal spend record.

        Args:
            collection: Collection that triggered the call.
            stage: Pipeline stage ('llm', 'tts', 'voice_design').
            backend: Engine that served the call.
            gpu_seconds: GPU time billed for the call.
            cost_usd: Cost in USD.
            run_id: Optional digest run id.
            audio_seconds: Optional audio duration produced.
            cache_hit: True when the call was served from cache (zero cost).
            wall_seconds: Optional client-observed wall-clock seconds.
            cost_source: How cost_usd was derived ('estimate' or 'runtime_rates';
                'billing_delta' is retired).
            gpu_type: Optional short GPU name ('l4', 'h100').
            billed_delta_usd: Retired billing-summary delta; always 0.
        """
        query = """
            INSERT INTO modal_spend (
                ts, collection, stage, backend, run_id,
                gpu_seconds, cost_usd, audio_seconds, cache_hit,
                wall_seconds, cost_source, gpu_type, billed_delta_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (
                    datetime.now(UTC).isoformat(),
                    collection,
                    stage,
                    backend,
                    run_id,
                    gpu_seconds,
                    cost_usd,
                    audio_seconds,
                    int(cache_hit),
                    wall_seconds,
                    cost_source,
                    gpu_type,
                    billed_delta_usd,
                ),
            )
            await db.commit()

    async def spent_since(self, since: str) -> float:
        """Return the total spend since an ISO-8601 timestamp.

        Args:
            since: Start of the window (UTC).

        Returns:
            Total cost in USD.
        """
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM modal_spend WHERE ts >= ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (since,)) as cursor:
                row = await cursor.fetchone()
                return float(row[0]) if row else 0.0

    async def last_call_ts(self) -> str | None:
        """Return the timestamp of the most recent Modal call.

        Returns:
            ISO-8601 timestamp, or None when no call has been recorded.
        """
        query = "SELECT ts FROM modal_spend ORDER BY ts DESC LIMIT 1"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                row = await cursor.fetchone()
                return str(row[0]) if row else None


class VerificationsRepository:
    """Repository for the LLM verification ledger (Phase 7.13).

    One row per (run_id, target, method): the digest is verified by the free
    ``groundedness`` check and by the LLM judge (``faithfulness``), so a run can
    accumulate several rows without overwriting each other.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def record(
        self,
        *,
        run_id: str,
        collection: str,
        target: str,
        method: str,
        verdict: str,
        score: float | None = None,
        threshold: float | None = None,
        passed: bool = False,
        details: dict[str, Any] | None = None,
        model: str | None = None,
        duration_seconds: float | None = None,
    ) -> int:
        """Persist one verification verdict and return its row id.

        Args:
            run_id: Workflow id of the digest run being verified.
            collection: Collection name the run belongs to.
            target: What was verified ('digest', 'narration_script', ...).
            method: Check that produced the score ('groundedness', 'faithfulness').
            verdict: 'valid', 'invalid' or 'error'.
            score: Measured score, or None when the check could not run.
            threshold: Threshold the score was compared against.
            passed: True when the score met the threshold.
            details: JSON breakdown of the verdict (claims, numbers, sources).
            model: Model or engine that produced the score.
            duration_seconds: Wall-clock time the check took.

        Returns:
            Id of the inserted row.
        """
        query = """
            INSERT INTO verifications (
                run_id, collection, target, method, verdict, score, threshold,
                passed, details, model, duration_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            run_id,
            collection,
            target,
            method,
            verdict,
            score,
            threshold,
            int(passed),
            json.dumps(details, sort_keys=True) if details is not None else None,
            model,
            duration_seconds,
        )
        async with get_db(self.db_path) as db:
            await db.execute(query, params)
            await db.commit()
            cursor = await db.execute("SELECT last_insert_rowid()")
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return every verification row of a run, oldest first.

        Args:
            run_id: Workflow id of the digest run.

        Returns:
            List of dicts with the columns of ``verifications`` plus a parsed
            ``details`` object (an empty dict when the column is NULL).
        """
        query = """
            SELECT id, run_id, collection, target, method, verdict, score,
                   threshold, passed, details, model, duration_seconds, created_at
            FROM verifications
            WHERE run_id = ?
            ORDER BY id ASC
        """
        rows: list[dict[str, Any]] = []
        async with get_db(self.db_path) as db:
            async with db.execute(query, (run_id,)) as cursor:
                async for row in cursor:
                    try:
                        details = json.loads(row["details"]) if row["details"] else {}
                    except ValueError:
                        details = {}
                    rows.append(
                        {
                            "id": row["id"],
                            "run_id": row["run_id"],
                            "collection": row["collection"],
                            "target": row["target"],
                            "method": row["method"],
                            "verdict": row["verdict"],
                            "score": row["score"],
                            "threshold": row["threshold"],
                            "passed": bool(row["passed"]),
                            "details": details,
                            "model": row["model"],
                            "duration_seconds": row["duration_seconds"],
                            "created_at": row["created_at"],
                        }
                    )
        return rows

    async def get_latest_for_run(self, run_id: str, target: str) -> dict[str, Any] | None:
        """Return the most recent verification row of a run for one target.

        Args:
            run_id: Workflow id of the digest run.
            target: Target to look up ('digest', 'narration_script', ...).

        Returns:
            The newest matching row as a dict, or None when there is none.
        """
        rows = await self.get_for_run(run_id)
        matching = [row for row in rows if row["target"] == target]
        return matching[-1] if matching else None


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp string into an aware UTC datetime.

    Rows written by this file use ``datetime.now(UTC).isoformat()`` (aware),
    while table defaults use ``CURRENT_TIMESTAMP`` (naive, UTC). Both are
    normalized here so expiry comparisons never raise.

    Args:
        value: Raw timestamp string from SQLite, or None.

    Returns:
        An aware UTC datetime, or None when value is None/empty.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class UsersRepository:
    """Repository for self-registered non-admin users (Phase 6, migration 012).

    The admin is a local credential in ``.env`` and never appears here.
    Disabling or deleting a user also purges their sessions so the change
    takes effect on the next request.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def create(self, email: str, role: str = "viewer") -> dict[str, Any]:
        """Insert a new applicant with status 'pending'.

        Args:
            email: Email of the applicant; stored case-insensitively unique.
            role: Role to assign once approved ('user' or 'viewer').

        Returns:
            The freshly inserted user row as a dict.

        Raises:
            ValueError: If the email is already registered.
        """
        now_iso = datetime.now(UTC).isoformat()
        normalized = email.strip()
        query = """
            INSERT INTO users (email, role, status, created_at)
            VALUES (?, ?, 'pending', ?)
        """
        async with get_db(self.db_path) as db:
            try:
                await db.execute(query, (normalized, role, now_iso))
            except aiosqlite.IntegrityError as exc:
                raise ValueError("email_already_registered") from exc
            async with db.execute("SELECT * FROM users WHERE email = ?", (normalized,)) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        return dict(row) if row else {}

    async def get(self, user_id: int) -> dict[str, Any] | None:
        """Return a user row by id, or None.

        Args:
            user_id: Primary key of the user.

        Returns:
            The user dict, or None when it does not exist.
        """
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_by_email(self, email: str) -> dict[str, Any] | None:
        """Return a user row by email (case-insensitive), or None.

        Args:
            email: Email to look up.

        Returns:
            The user dict, or None when it does not exist.
        """
        async with get_db(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM users WHERE email = ?", (email.strip(),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        """List every user, newest registration first.

        Returns:
            List of user dicts ordered by id descending.
        """
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT * FROM users ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def count(self) -> int:
        """Count users of any status.

        Returns:
            Number of registered (non-admin) users.
        """
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0

    async def set_status(self, user_id: int, status: str, role: str | None = None) -> None:
        """Update a user's status and, optionally, their role.

        Approving (status 'active') stamps ``approved_at`` on first approval.
        Any status other than 'active' purges the user's open sessions.

        Args:
            user_id: Primary key of the user to update.
            status: New status ('pending' | 'active' | 'disabled').
            role: Optional new role ('user' | 'viewer'); None keeps current.

        Raises:
            ValueError: If no row matched user_id.
        """
        now_iso = datetime.now(UTC).isoformat()
        async with get_db(self.db_path) as db:
            if role is None:
                query = """
                    UPDATE users
                    SET status = ?,
                        approved_at = CASE
                            WHEN status <> 'active' AND ? = 'active'
                                 THEN COALESCE(approved_at, ?)
                            ELSE approved_at
                        END
                    WHERE id = ?
                """
                params: tuple[Any, ...] = (status, status, now_iso, user_id)
            else:
                query = """
                    UPDATE users
                    SET status = ?, role = ?,
                        approved_at = CASE
                            WHEN status <> 'active' AND ? = 'active'
                                 THEN COALESCE(approved_at, ?)
                            ELSE approved_at
                        END
                    WHERE id = ?
                """
                params = (status, role, status, now_iso, user_id)
            cursor = await db.execute(query, params)
            if cursor.rowcount == 0:
                raise ValueError("user_not_found")
            if status != "active":
                await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            await db.commit()

    async def delete(self, user_id: int) -> None:
        """Delete a user and all their sessions.

        Args:
            user_id: Primary key of the user to remove.

        Raises:
            ValueError: If no row matched user_id.
        """
        async with get_db(self.db_path) as db:
            # Purge sessions first: FK ON DELETE SET NULL would null user_id
            # and make a post-delete purge match nothing.
            await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            if cursor.rowcount == 0:
                raise ValueError("user_not_found")
            await db.commit()

    async def touch_login(self, user_id: int) -> None:
        """Stamp ``last_login_at`` after a successful OTP login.

        Args:
            user_id: Primary key of the user who just logged in.
        """
        now_iso = datetime.now(UTC).isoformat()
        async with get_db(self.db_path) as db:
            await db.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now_iso, user_id),
            )
            await db.commit()


class LoginCodesRepository:
    """Repository for sha256-hashed one-time login codes (migration 012).

    Only the hash of the 6-digit code is persisted. At most one live code
    exists per email: creating a new one purges previous rows, which also
    keeps the resend-cooldown check trivial (look at the single row).
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def create(self, email: str, code_hash: str, ttl_seconds: int = 600) -> dict[str, Any]:
        """Store a fresh code hash, replacing any previous codes for the email.

        Args:
            email: Email the code was sent to.
            code_hash: Hex sha256 digest of the 6-digit code.
            ttl_seconds: Lifetime of the code in seconds (default 600 = 10 min).

        Returns:
            The freshly inserted login code row as a dict.
        """
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        normalized = email.strip()
        async with get_db(self.db_path) as db:
            await db.execute("DELETE FROM login_codes WHERE email = ?", (normalized,))
            await db.execute(
                """
                INSERT INTO login_codes
                    (email, code_hash, attempts, expires_at, last_sent_at,
                     created_at)
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (normalized, code_hash, expires_iso, now_iso, now_iso),
            )
            async with db.execute(
                "SELECT * FROM login_codes WHERE email = ?", (normalized,)
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        return dict(row) if row else {}

    async def latest(self, email: str) -> dict[str, Any] | None:
        """Return the newest code row for an email, or None.

        Args:
            email: Email whose live code to fetch.

        Returns:
            The login code dict, or None when no code was ever issued.
        """
        async with get_db(self.db_path) as db:
            async with db.execute(
                """
                SELECT * FROM login_codes
                WHERE email = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (email.strip(),),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def record_attempt(self, code_id: int) -> int:
        """Increment the failed-attempts counter of a code.

        Args:
            code_id: Primary key of the code row.

        Returns:
            The attempts count after the increment.
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                "UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?",
                (code_id,),
            )
            async with db.execute(
                "SELECT attempts FROM login_codes WHERE id = ?", (code_id,)
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return int(row[0]) if row else 0

    async def consume(self, code_id: int) -> bool:
        """Atomically mark a code as used (single-use guarantee).

        Args:
            code_id: Primary key of the code row.

        Returns:
            True if this call consumed the code, False if it was already used.
        """
        now_iso = datetime.now(UTC).isoformat()
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE login_codes
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (now_iso, code_id),
            )
            consumed = cursor.rowcount > 0
            await db.commit()
            return consumed

    async def purge_expired(self) -> int:
        """Delete codes whose expiry has passed.

        Returns:
            Number of rows removed.
        """
        now_iso = datetime.now(UTC).isoformat()
        async with get_db(self.db_path) as db:
            cursor = await db.execute("DELETE FROM login_codes WHERE expires_at < ?", (now_iso,))
            await db.commit()
            return cursor.rowcount

    async def purge_for_email(self, email: str) -> None:
        """Delete every code of an email (e.g. after a failed send).

        Args:
            email: Email whose codes should be removed.
        """
        async with get_db(self.db_path) as db:
            await db.execute("DELETE FROM login_codes WHERE email = ?", (email.strip(),))
            await db.commit()

    async def is_expired(self, row: dict[str, Any]) -> bool:
        """Tell whether a code row is past its expiry.

        Args:
            row: Login code dict as returned by ``latest``/``create``.

        Returns:
            True when the code can no longer be verified.
        """
        expires_at = _parse_ts(row.get("expires_at"))
        if expires_at is None:
            return True
        return datetime.now(UTC) >= expires_at


class SessionsRepository:
    """Repository for HttpOnly session tokens (migration 012 ``sessions``).

    Replaces the legacy cookie that carried the raw admin password: each
    login inserts a random ``secrets.token_urlsafe(32)`` token that the
    cookie stores verbatim, so the DB only ever holds that token plus the
    role snapshot used to authorize requests.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    async def create(
        self,
        token: str,
        identity: str,
        role: str,
        user_id: int | None = None,
        ttl_hours: int = 12,
    ) -> dict[str, Any]:
        """Insert a new session and purge expired ones.

        Args:
            token: Random URL-safe token stored verbatim in the cookie.
            identity: Display identity (email or admin username).
            role: Role snapshot ('admin' | 'user' | 'viewer').
            user_id: users.id for non-admin sessions, None for the admin.
            ttl_hours: Session lifetime in hours (matches the cookie max-age).

        Returns:
            The freshly inserted session row as a dict.
        """
        now = datetime.now(UTC)
        expires_iso = (now + timedelta(hours=ttl_hours)).isoformat()
        async with get_db(self.db_path) as db:
            await db.execute(
                "DELETE FROM sessions WHERE expires_at < ?",
                (now.isoformat(),),
            )
            await db.execute(
                """
                INSERT INTO sessions
                    (token, user_id, identity, role, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token, user_id, identity, role, now.isoformat(), expires_iso),
            )
            async with db.execute("SELECT * FROM sessions WHERE token = ?", (token,)) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        return dict(row) if row else {}

    async def get(self, token: str) -> dict[str, Any] | None:
        """Return a live session by token, or None.

        Expired sessions are deleted on lookup so stale cookies cannot be
        replayed after their 12 h window.

        Args:
            token: Raw token read from the cookie.

        Returns:
            The session dict, or None when missing or expired.
        """
        if not token:
            return None
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT * FROM sessions WHERE token = ?", (token,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            session = dict(row)
            expires_at = _parse_ts(session.get("expires_at"))
            if expires_at is None or datetime.now(UTC) >= expires_at:
                await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
                await db.commit()
                return None
            return session

    async def delete(self, token: str) -> None:
        """Delete one session (logout).

        Args:
            token: Raw token read from the cookie.
        """
        async with get_db(self.db_path) as db:
            await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            await db.commit()

    async def delete_for_user(self, user_id: int) -> None:
        """Delete every session of a user (revoke access).

        Args:
            user_id: Primary key of the user.
        """
        async with get_db(self.db_path) as db:
            await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            await db.commit()

    async def purge_expired(self) -> int:
        """Delete all expired sessions.

        Returns:
            Number of rows removed.
        """
        now_iso = datetime.now(UTC).isoformat()
        async with get_db(self.db_path) as db:
            cursor = await db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
            await db.commit()
            return cursor.rowcount


class PhoneticHeuristicsRepository:
    """Repository for the phonetic-adaptation heuristics (migration 015).

    One row per ``(language_origin, language_target)`` pair: how the respeller
    recognises foreign terms of the origin language inside text read in the
    target language. The seeds come from the migration; the admin GUI at
    ``/ui/phonetics`` edits them.
    """

    #: Columns stored as JSON arrays of strings.
    _LIST_COLUMNS = ("suffixes", "digraphs", "tech_terms", "native_words")

    def __init__(self, db_path: str | None = None) -> None:
        """Initialize with an optional database path.

        Args:
            db_path: Path to the SQLite database. Defaults to settings.database_path.
        """
        self.db_path = db_path

    def _decode(self, row: Any) -> dict[str, Any]:
        """Turn one row into a dict with JSON columns parsed and flags as bools.

        Args:
            row: An aiosqlite.Row (or mapping) of ``phonetic_heuristics``.

        Returns:
            The decoded row dict.
        """
        data = dict(row)
        for column in self._LIST_COLUMNS:
            raw = data.get(column) or "[]"
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = []
            data[column] = [str(item) for item in parsed] if isinstance(parsed, list) else []
        raw_examples = data.get("examples") or "{}"
        try:
            examples = json.loads(raw_examples)
        except (TypeError, ValueError):
            examples = {}
        data["examples"] = examples if isinstance(examples, dict) else {}
        for flag in ("ascii_only", "flag_all_foreign", "enabled"):
            data[flag] = bool(int(data.get(flag) or 0))
        for stamp in ("created_at", "updated_at"):
            data[stamp] = str(data.get(stamp) or "")
        return data

    @staticmethod
    def _encode(data: dict[str, Any]) -> dict[str, Any]:
        """Serialize the JSON/bool columns of a payload for SQLite.

        Args:
            data: Payload with Python lists, dicts and bools.

        Returns:
            The payload with JSON strings and 0/1 integers.
        """
        encoded = dict(data)
        for column in PhoneticHeuristicsRepository._LIST_COLUMNS:
            encoded[column] = json.dumps(list(encoded.get(column) or []), ensure_ascii=False)
        encoded["examples"] = json.dumps(dict(encoded.get("examples") or {}), ensure_ascii=False)
        for flag in ("ascii_only", "flag_all_foreign", "enabled"):
            encoded[flag] = 1 if encoded.get(flag, True) else 0
        return encoded

    async def list_all(self) -> list[dict[str, Any]]:
        """Return every row ordered by the language pair.

        Returns:
            Decoded row dicts, sorted by origin then target.
        """
        query = "SELECT * FROM phonetic_heuristics ORDER BY language_origin, language_target"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
        return [self._decode(row) for row in rows]

    async def get(self, row_id: int) -> dict[str, Any] | None:
        """Return one row by id.

        Args:
            row_id: Primary key.

        Returns:
            The decoded row, or None when missing.
        """
        async with get_db(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM phonetic_heuristics WHERE id = ?", (row_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._decode(row) if row else None

    async def get_pair(self, language_origin: str, language_target: str) -> dict[str, Any] | None:
        """Return the row of one language pair, or None.

        Args:
            language_origin: ISO 639-1 language of the terms.
            language_target: ISO 639-1 language reading them.

        Returns:
            The decoded row, or None when the pair is unconfigured.
        """
        query = """
            SELECT * FROM phonetic_heuristics
            WHERE language_origin = ? AND language_target = ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (language_origin, language_target)) as cursor:
                row = await cursor.fetchone()
        return self._decode(row) if row else None

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert one row and return it.

        Args:
            data: Validated payload (lists/dicts/bools).

        Returns:
            The inserted row, decoded.

        Raises:
            ValueError: When the pair already exists (UNIQUE violation).
        """
        encoded = self._encode(data)
        columns = ", ".join(encoded)
        placeholders = ", ".join("?" for _ in encoded)
        query = f"INSERT INTO phonetic_heuristics ({columns}) VALUES ({placeholders})"
        async with get_db(self.db_path) as db:
            try:
                await db.execute(query, tuple(encoded.values()))
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                raise ValueError(
                    f"a heuristics row for {data['language_origin']} -> "
                    f"{data['language_target']} already exists"
                ) from exc
            async with db.execute(
                "SELECT * FROM phonetic_heuristics WHERE rowid = last_insert_rowid()"
            ) as cursor:
                row = await cursor.fetchone()
        return self._decode(row) if row else {}

    async def update(self, row_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        """Replace the editable columns of one row.

        Args:
            row_id: Primary key of the row.
            data: Validated payload (lists/dicts/bools).

        Returns:
            The updated row, or None when it does not exist.

        Raises:
            ValueError: When another row already owns the pair (UNIQUE violation).
        """
        encoded = self._encode(data)
        assignments = ", ".join(f"{column} = ?" for column in encoded)
        query = (
            f"UPDATE phonetic_heuristics SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?"
        )
        async with get_db(self.db_path) as db:
            try:
                cursor = await db.execute(query, (*encoded.values(), row_id))
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                raise ValueError(
                    f"a heuristics row for {data['language_origin']} -> "
                    f"{data['language_target']} already exists"
                ) from exc
            if cursor.rowcount == 0:
                return None
            async with db.execute(
                "SELECT * FROM phonetic_heuristics WHERE id = ?", (row_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._decode(row) if row else None

    async def delete(self, row_id: int) -> bool:
        """Delete one row.

        Args:
            row_id: Primary key of the row.

        Returns:
            True when a row was removed, False when it did not exist.
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute("DELETE FROM phonetic_heuristics WHERE id = ?", (row_id,))
            await db.commit()
            return bool(cursor.rowcount)
