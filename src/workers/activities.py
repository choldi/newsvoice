"""Temporal activities wrapping the news-engine services."""

import asyncio
import time
from pathlib import Path
from typing import Any

import feedparser
import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository, RunsRepository
from src.api.models.article import ArticleIn, DigestIn
from src.api.models.cluster import ClusterIn
from src.api.models.tts import TTSIn
from src.api.services.cluster import ClusterService
from src.api.services.dedup import DedupService
from src.api.services.digest import DigestService
from src.api.services.email import EmailService
from src.api.services.tts import TTSService
from src.core.config import get_settings
from src.core.prompts import strip_emotion_tags

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
# Telegram hard limit is 4096 chars per sendMessage; keep a safety margin.
TELEGRAM_MESSAGE_MAX_CHARS = 4000
FEED_FETCH_TIMEOUT_SECONDS = 60


def split_message_chunks(text: str, max_chars: int = TELEGRAM_MESSAGE_MAX_CHARS) -> list[str]:
    """Split long text into Telegram-safe message chunks.

    Args:
        text: The full message body to send.
        max_chars: Maximum number of characters per chunk.

    Returns:
        List of trimmed chunks. Prefers splitting at paragraph breaks so each
        chunk stays coherent when rendered with Telegram's Markdown parse mode.
    """
    remaining = text.strip()
    if len(remaining) <= max_chars:
        return [remaining] if remaining else []

    chunks: list[str] = []
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n\n", 0, max_chars)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, max_chars)
        if split_at <= 0:
            split_at = max_chars
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


@activity.defn
async def fetch_rss_activity(feed_urls: list[str]) -> list[dict[str, Any]]:
    """Fetch and normalize articles from a list of RSS feeds.

    Each feed is fetched independently; a failure on one feed only logs a
    warning and skips that feed rather than aborting the whole batch.

    Args:
        feed_urls: List of RSS feed URLs to parse.

    Returns:
        List of article dicts compatible with the ArticleIn schema.
    """
    activity.logger.info(f"Fetching RSS feeds: {len(feed_urls)} feeds")
    articles: list[dict[str, Any]] = []
    for url in feed_urls:
        try:
            feed = await asyncio.wait_for(
                asyncio.to_thread(feedparser.parse, url),
                timeout=FEED_FETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            activity.logger.warning(f"feed_fetch_failed: {url} ({exc})")
            continue

        entries = getattr(feed, "entries", []) or []
        if getattr(feed, "bozo", 0) and getattr(feed, "bozo_exception", None):
            activity.logger.warning(f"feed_has_parse_errors: {url} ({feed.bozo_exception})")

        feed_obj = getattr(feed, "feed", None)
        source = getattr(feed_obj, "title", None) or url
        for entry in entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue
            content = entry.get("summary") or entry.get("description") or ""
            articles.append(
                {
                    "title": title,
                    "link": link,
                    "content": content,
                    "pub_date": entry.get("published") or entry.get("updated"),
                    "source": source,
                }
            )
    activity.logger.info(f"RSS fetch completed: {len(articles)} articles")
    return articles


def _validate_articles(articles: list[dict[str, Any]]) -> list[ArticleIn]:
    """Validate a batch of article dicts, skipping malformed items.

    Args:
        articles: List of raw article dicts.

    Returns:
        List of validated ArticleIn models.
    """
    validated: list[ArticleIn] = []
    for item in articles:
        try:
            validated.append(ArticleIn.model_validate(item))
        except ValidationError as exc:
            activity.logger.warning(f"article_validation_failed: {exc}")
    return validated


@activity.defn
async def filter_new_activity(
    articles: list[dict[str, Any]], collection: str = "default"
) -> list[dict[str, Any]]:
    """Filter out articles already delivered within the retention window.

    Args:
        articles: List of article dicts to deduplicate.
        collection: Collection name scoping the dedup window.

    Returns:
        List of new unseen article dicts.

    Raises:
        ApplicationError: If deduplication fails for a non-validation reason.
    """
    activity.logger.info(f"Filtering new articles: {len(articles)}, collection={collection}")
    validated = _validate_articles(articles)
    if not validated:
        return []
    try:
        new_articles = await DedupService().filter_new_articles(validated, collection=collection)
    except Exception as exc:
        raise ApplicationError(f"Deduplication failed: {exc}", non_retryable=True) from exc
    return [a.model_dump() for a in new_articles]


@activity.defn
async def cluster_activity(
    articles: list[dict[str, Any]], collection: str = "default"
) -> list[dict[str, Any]]:
    """Group articles into event clusters.

    Args:
        articles: List of article dicts to cluster.
        collection: Collection name scoping cluster state.

    Returns:
        List of cluster dicts conforming to the ClusterItem schema.

    Raises:
        ApplicationError: If clustering fails for a non-validation reason.
    """
    activity.logger.info(f"Clustering articles: {len(articles)}, collection={collection}")
    validated = _validate_articles(articles)
    if not validated:
        return []
    try:
        clusters = await ClusterService().cluster_articles(
            ClusterIn(articles=validated, collection=collection)
        )
    except Exception as exc:
        raise ApplicationError(f"Clustering failed: {exc}", non_retryable=True) from exc
    return [c.model_dump() for c in clusters]


@activity.defn
async def generate_digest_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate the AI news digest from RSS articles/clusters.

    Args:
        payload: Dict matching the DigestIn schema (articles and/or clusters).

    Returns:
        Dict with status, digest markdown, and article_count.

    Raises:
        ApplicationError: If digest generation fails.
    """
    activity.logger.info("Generating digest")
    try:
        settings = get_settings()
        digest_in = DigestIn.model_validate(payload)
        if not digest_in.podcast_behavior:
            digest_in.podcast_behavior = settings.tts_podcast_behavior
        if not digest_in.podcast_name:
            digest_in.podcast_name = settings.tts_podcast_name
        if not digest_in.language:
            digest_in.language = settings.tts_language
        result = await DigestService().generate_digest(digest_in)
    except Exception as exc:
        raise ApplicationError(f"Digest generation failed: {exc}", non_retryable=True) from exc
    return result.model_dump()


@activity.defn
async def generate_tts_activity(digest: dict[str, Any]) -> dict[str, Any]:
    """Convert the digest markdown into MP3 audio.

    Records the generation progress in the audio_jobs table so clients can
    query STARTING/GENERATING/FINISHED_OK/FINISHED_ERROR status, the error
    detail, and elapsed time.

    Args:
        digest: Dict containing the digest text under the 'digest' key, optional
            'language'/'voice' overrides for this podcast, plus 'run_id' and
            'collection' used to key the audio job status.

    Returns:
        Dict with audio_path, duration_estimate, cached, voice, and char_count.

    Raises:
        ApplicationError: If the digest text is missing or TTS synthesis fails.
    """
    activity.logger.info("Generating TTS audio")
    markdown = digest.get("digest") if isinstance(digest, dict) else None
    if not markdown:
        raise ApplicationError("Digest text missing for TTS", non_retryable=True)
    run_id = str(digest.get("run_id") or activity.info().activity_id)
    collection = str(digest.get("collection") or "default")

    audio_repo = AudioJobsRepository()
    await audio_repo.start_job(run_id, collection)
    started = time.monotonic()
    try:
        settings = get_settings()
        language = digest.get("language") or settings.tts_language
        voice = digest.get("voice") or settings.tts_voice
        tts_in = TTSIn(markdown=str(markdown), language=language, voice=voice)
        await audio_repo.set_generating(run_id)
        result = await TTSService().generate_tts(tts_in)
    except Exception as exc:
        await audio_repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        raise ApplicationError(f"TTS generation failed: {exc}", non_retryable=True) from exc
    await audio_repo.finish_job(
        run_id,
        status="FINISHED_OK",
        audio_path=result.audio_path,
        elapsed_seconds=round(time.monotonic() - started, 2),
    )
    return result.model_dump()


async def _telegram_request(
    method: str,
    payload: dict[str, Any],
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Call the Telegram Bot API and normalize the response.

    Args:
        method: Telegram API method name (e.g. sendMessage, sendAudio).
        payload: JSON body for the request.
        chat_id: Optional per-collection chat id; falls back to settings.

    Returns:
        Dict with status and Telegram message id.

    Raises:
        ApplicationError: If Telegram credentials are missing, or the API
            returns an error or an unexpected response shape.
    """
    settings = get_settings()
    target_chat = chat_id or settings.telegram_chat_id
    if not settings.telegram_bot_token or not target_chat:
        raise ApplicationError(
            "Telegram bot token and chat id must be configured",
            non_retryable=True,
        )
    url = TELEGRAM_API_URL.format(token=settings.telegram_bot_token, method=method)
    body = dict(payload)
    body["chat_id"] = target_chat
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=body)
        try:
            data = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise ApplicationError(
                f"Telegram returned a non-JSON response: {exc}",
                non_retryable=True,
            ) from exc
    if not response.is_success or not data.get("ok"):
        description = data.get("description") or data
        raise ApplicationError(f"Telegram API error: {description}", non_retryable=True)
    result = data.get("result") or {}
    message_id = result.get("message_id")
    if message_id is None:
        raise ApplicationError(
            f"Telegram response missing message_id: {data}",
            non_retryable=True,
        )
    return {"status": "ok", "message_id": message_id}


@activity.defn
async def record_run_activity(run: dict[str, Any]) -> dict[str, Any]:
    """Persist a run summary to the digest_runs table for the admin GUI.

    Args:
        run: Dict with workflow_id, collection, status, article_count,
            cluster_count, digest_preview, audio_path, and error.

    Returns:
        Dict with status and the recorded run id.

    Raises:
        ApplicationError: If recording the run fails.
    """
    activity.logger.info(
        "Recording run",
        collection=run.get("collection"),
        status=run.get("status"),
        workflow_id=run.get("workflow_id"),
    )
    try:
        run_id = await RunsRepository().record_run(**run)
    except Exception as exc:
        raise ApplicationError(f"Recording run failed: {exc}", non_retryable=True) from exc
    return {"status": "ok", "run_id": run_id}


@activity.defn
async def deliver_telegram_message_activity(
    text: str, chat_id: str | None = None
) -> dict[str, Any]:
    """Send the digest text via Telegram.

    Args:
        text: The digest markdown text to send.
        chat_id: Optional per-collection chat id; falls back to settings.

    Returns:
        Dict with status and Telegram message id.
    """
    activity.logger.info("Sending Telegram message")
    chunks = split_message_chunks(strip_emotion_tags(text))
    if not chunks:
        raise ApplicationError("Telegram message is empty", non_retryable=True)
    results = []
    for chunk in chunks:
        try:
            results.append(
                await _telegram_request(
                    "sendMessage", {"text": chunk, "parse_mode": "Markdown"}, chat_id=chat_id
                )
            )
        except ApplicationError:
            activity.logger.warning(
                f"telegram_markdown_rejected_sending_plain_text chunk_len={len(chunk)}"
            )
            results.append(await _telegram_request("sendMessage", {"text": chunk}, chat_id=chat_id))
    if len(results) == 1:
        return results[0]
    return {"status": "ok", "message_ids": [r["message_id"] for r in results]}


def _audio_mime_type(audio_path: str) -> str:
    """Return the audio MIME type based on the file extension.

    Args:
        audio_path: Path to the audio file.

    Returns:
        MIME type string (defaults to audio/mpeg for unknown extensions).
    """
    suffix = Path(audio_path).suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".ogg":
        return "audio/ogg"
    return "audio/mpeg"


@activity.defn
async def deliver_telegram_audio_activity(
    audio_path: str, caption: str | None = None, chat_id: str | None = None
) -> dict[str, Any]:
    """Send a generated audio file via Telegram.

    Args:
        audio_path: Path to the MP3 file on disk.
        caption: Optional caption text for the audio message.
        chat_id: Optional per-collection chat id; falls back to settings.

    Returns:
        Dict with status and Telegram message id.

    Raises:
        ApplicationError: If credentials are missing, the file does not exist,
            or the API returns an error.
    """
    activity.logger.info(f"Sending Telegram audio: {audio_path}")
    settings = get_settings()
    target_chat = chat_id or settings.telegram_chat_id
    if not settings.telegram_bot_token or not target_chat:
        raise ApplicationError(
            "Telegram bot token and chat id must be configured",
            non_retryable=True,
        )
    try:
        audio_file = open(audio_path, "rb")
    except OSError as exc:
        raise ApplicationError(
            f"Cannot open audio file {audio_path}: {exc}", non_retryable=True
        ) from exc
    audio_mime = _audio_mime_type(audio_path)
    with audio_file:
        files = {"audio": (audio_path.rsplit("/", 1)[-1], audio_file, audio_mime)}
        caption = strip_emotion_tags(caption) if caption else None
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                TELEGRAM_API_URL.format(token=settings.telegram_bot_token, method="sendAudio"),
                data={"chat_id": target_chat, "caption": caption},
                files=files,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise ApplicationError(
                    f"Telegram returned a non-JSON response: {exc}",
                    non_retryable=True,
                ) from exc
    if not data.get("ok"):
        raise ApplicationError(f"Telegram API error: {data}", non_retryable=True)
    result = data.get("result") or {}
    message_id = result.get("message_id")
    if message_id is None:
        raise ApplicationError(
            f"Telegram response missing message_id: {data}",
            non_retryable=True,
        )
    return {"status": "ok", "message_id": message_id}


@activity.defn
async def deliver_email_activity(
    recipients: list[str],
    markdown_text: str,
    collection: str,
    audio_path: str | None = None,
) -> dict[str, Any]:
    """Send the digest as an HTML email to a list of recipients.

    Args:
        recipients: Destination email addresses.
        markdown_text: Raw digest markdown content.
        collection: Collection name (used in subject and header).
        audio_path: Optional audio file to attach to the email.

    Returns:
        Dict with status and recipient count.

    Raises:
        ApplicationError: If the email service is not configured or SMTP
            fails to deliver the message.
    """
    activity.logger.info(
        f"Sending digest email: collection={collection}, recipients={len(recipients)}"
    )
    try:
        result = await EmailService().send_digest_email(
            recipients,
            markdown_text,
            collection,
            audio_path=audio_path,
        )
    except Exception as exc:
        raise ApplicationError(f"Email delivery failed: {exc}", non_retryable=True) from exc
    return result
