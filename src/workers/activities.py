"""Temporal activities wrapping the news-engine services."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import feedparser
import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository, RunsRepository, SyncStateRepository
from src.api.models.article import ArticleIn, DigestIn
from src.api.models.cluster import ClusterIn, ClusterItem
from src.api.models.tts import TTSIn
from src.api.services.audio_compress import AudioCompressService
from src.api.services.cluster import ClusterService
from src.api.services.dedup import DedupService
from src.api.services.digest import DigestService
from src.api.services.email import EmailService
from src.api.services.tts import TTSService
from src.api.services.whatsapp import (
    WhatsAppConfigError,
    WhatsAppNotAllowedError,
    WhatsAppService,
)
from src.core.config import get_settings
from src.core.prompts import strip_emotion_tags

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"
# Telegram hard limit is 4096 chars per sendMessage; keep a safety margin.
TELEGRAM_MESSAGE_MAX_CHARS = 4000
FEED_FETCH_TIMEOUT_SECONDS = 60
# Key under which SyncStateRepository remembers the last registered signature.
COLLECTIONS_SIGNATURE_KEY = "collections_signature"
# Key under which SyncStateRepository remembers the enabled collection names so
# the collections-config monitor can report added/removed collections.
COLLECTIONS_NAMES_KEY = "collections_names"


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
async def generate_logic_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask the LLM to explain the news-selection logic behind a digest.

    Runs a separate LLM call (temperature 0.3) anchored on the story clusters
    and the final digest. Only called by the workflow when a collection defines
    a ``logic_delivery`` destination.

    Args:
        payload: Dict with 'clusters' (list of ClusterItem dicts), 'digest'
            (markdown), 'collection', 'prompt_template', 'custom_prompt',
            'language', 'main_story_count', and 'headline_count'.

    Returns:
        Dict with status and the generated 'logic' text.

    Raises:
        ApplicationError: If the digest text is missing or generation fails.
    """
    activity.logger.info("Generating selection logic")
    digest = payload.get("digest")
    if not digest:
        raise ApplicationError("Digest text missing for logic generation", non_retryable=True)
    clusters: list[ClusterItem] = []
    for item in payload.get("clusters") or []:
        try:
            clusters.append(ClusterItem.model_validate(item))
        except ValidationError as exc:
            activity.logger.warning(f"logic_cluster_validation_failed: {exc}")
    try:
        logic = await DigestService().generate_selection_logic(
            clusters=clusters,
            digest=str(digest),
            prompt_template=payload.get("prompt_template") or "default",
            custom_prompt=payload.get("custom_prompt") or "",
            language=payload.get("language") or "",
            main_story_count=payload.get("main_story_count", 5),
            headline_count=payload.get("headline_count", 5),
        )
    except Exception as exc:
        raise ApplicationError(
            f"Selection logic generation failed: {exc}", non_retryable=True
        ) from exc
    return {"status": "ok", "logic": logic}


@activity.defn
async def deliver_logic_activity(
    logic: str, delivery: dict[str, Any], collection: str
) -> dict[str, Any]:
    """Send the LLM selection-logic text to the configured admin destinations.

    Each configured channel is attempted independently: a failure on one
    channel is recorded in that channel's entry without aborting the others.

    Args:
        logic: The LLM-generated selection-logic text.
        delivery: Dict matching LogicDeliveryConfig (channels, chat_id,
            email_recipients, whatsapp_recipients, whatsapp_group_id).
        collection: Collection name (used in the email subject).

    Returns:
        Dict with status ok and a 'deliveries' list, one entry per channel.
    """
    activity.logger.info(f"Delivering selection logic: collection={collection}")
    channels = delivery.get("channels") or ["telegram"]
    deliveries: list[dict[str, Any]] = []

    if "telegram" in channels:
        try:
            deliveries.append(
                {
                    "channel": "telegram",
                    **await deliver_telegram_message_activity(logic, delivery.get("chat_id")),
                }
            )
        except Exception as exc:
            deliveries.append({"channel": "telegram", "status": "error", "error": str(exc)})

    if "email" in channels and delivery.get("email_recipients"):
        subject = (
            f"News Selection Logic \u00b7 {collection} \u00b7 "
            f"{datetime.now(UTC).strftime('%Y-%m-%d')}"
        )
        try:
            deliveries.append(
                {
                    "channel": "email",
                    **await EmailService().send_digest_email(
                        delivery["email_recipients"], logic, collection, subject=subject
                    ),
                }
            )
        except Exception as exc:
            deliveries.append({"channel": "email", "status": "error", "error": str(exc)})

    if "whatsapp" in channels and (
        delivery.get("whatsapp_recipients") or delivery.get("whatsapp_group_id")
    ):
        try:
            deliveries.append(
                {
                    "channel": "whatsapp",
                    **await deliver_whatsapp_message_activity(
                        logic,
                        delivery.get("whatsapp_recipients"),
                        delivery.get("whatsapp_group_id"),
                    ),
                }
            )
        except Exception as exc:
            deliveries.append({"channel": "whatsapp", "status": "error", "error": str(exc)})

    return {"status": "ok", "deliveries": deliveries}


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
        tts_in = TTSIn(
            markdown=str(markdown), language=language, voice=voice, collection=collection
        )
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


@activity.defn
async def compress_audio_activity(audio_path: str) -> dict[str, Any]:
    """Transcode the synthesized WAV to a delivery-ready M4A (AAC) file.

    Telegram caps uploads at 50 MB and email inboxes struggle with multi-hundred
    MB WAV files; this step compresses the audio before any delivery activity
    sends it. A mono AAC M4A plays on Telegram, WhatsApp, Android, and iPhone.

    Args:
        audio_path: Path to the WAV file produced by generate_tts_activity.

    Returns:
        Dict with the compressed audio_path and a cached flag.

    Raises:
        ApplicationError: If the source is missing or transcoding fails.
    """
    activity.logger.info(f"Compressing audio: {audio_path}")
    try:
        compressed_path = await AudioCompressService().compress(audio_path)
    except Exception as exc:
        raise ApplicationError(f"Audio compression failed: {exc}", non_retryable=True) from exc
    return {"audio_path": compressed_path, "cached": Path(compressed_path).exists()}


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
    if suffix == ".m4a":
        return "audio/mp4"
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


@activity.defn
async def deliver_whatsapp_message_activity(
    text: str,
    recipients: list[str] | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Send the digest text via WhatsApp (Baileys gateway).

    Fail-closed: unconfigured gateway or a recipient missing from the allowlist
    raises a non-retryable error; the workflow records it and continues with the
    other channels.

    Args:
        text: The digest markdown text to send.
        recipients: Optional E.164 numbers (fall back to none).
        group_id: Optional WhatsApp group id.

    Returns:
        Dict with status and recipient count.

    Raises:
        ApplicationError: If the gateway is unconfigured, a recipient is not
            allowlisted, or a send fails.
    """
    activity.logger.info("Sending WhatsApp message")
    try:
        result = await WhatsAppService().send_text(text, recipients or [], group_id=group_id)
    except (WhatsAppConfigError, WhatsAppNotAllowedError) as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception as exc:
        raise ApplicationError(f"WhatsApp delivery failed: {exc}") from exc
    return result


@activity.defn
async def deliver_whatsapp_audio_activity(
    audio_path: str,
    caption: str | None = None,
    recipients: list[str] | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Send a generated audio file via WhatsApp (Baileys gateway).

    Args:
        audio_path: Path to the compressed M4A file on disk.
        caption: Optional caption text for the audio message.
        recipients: Optional E.164 numbers (fall back to none).
        group_id: Optional WhatsApp group id.

    Returns:
        Dict with status and recipient count.

    Raises:
        ApplicationError: If the gateway is unconfigured, a recipient is not
            allowlisted, the file is missing or oversized, or a send fails.
    """
    activity.logger.info(f"Sending WhatsApp audio: {audio_path}")
    try:
        result = await WhatsAppService().send_audio(
            audio_path,
            recipients or [],
            group_id=group_id,
            caption=caption,
        )
    except (WhatsAppConfigError, WhatsAppNotAllowedError) as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception as exc:
        raise ApplicationError(f"WhatsApp audio delivery failed: {exc}") from exc
    return result


@activity.defn
async def read_collections_signature_activity() -> str | None:
    """Compute the sha256 signature of the current collections file.

    The file must parse cleanly; a missing, unreadable, or malformed file
    yields None so the monitor skips syncing until it is fixed.

    Returns:
        sha256 hex digest of the raw file contents, or None if it cannot
        be read or parsed as valid collections.toml.
    """
    from src.core.collections import CollectionsLoader

    settings = get_settings()
    loader = CollectionsLoader(path=settings.collections_file)
    try:
        loader.load()
    except Exception as exc:
        activity.logger.error(f"collections_signature_parse_failed: {exc}")
        return None

    path = Path(settings.collections_file)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        activity.logger.error(f"collections_signature_read_failed: {exc}")
        return None
    activity.logger.info(f"collections_signature_computed: {digest[:12]}")
    return digest


@activity.defn
async def get_synced_signature_activity() -> str | None:
    """Return the signature of the last successfully registered state.

    Returns:
        The stored signature, or None if nothing has been registered yet.
    """
    return await SyncStateRepository().get(COLLECTIONS_SIGNATURE_KEY)


@activity.defn
async def sync_collections_schedules_activity(signature: str) -> None:
    """Re-register the Temporal schedules from the current collections file.

    Reconciles every `daily-digest-*` schedule with the collections currently
    enabled in collections.toml (updating/removing as needed), guarantees the
    monitor schedules exist, then stores the signature and the enabled
    collection names so the monitors can detect the next change.

    Args:
        signature: sha256 of the collections file just registered.

    Raises:
        ActivityError: If schedule reconciliation or registration fails; the
            signature is only stored after a successful run.
    """
    from src.api.services.temporal_admin import TemporalAdminService
    from src.core.collections import CollectionsLoader, default_collection
    from src.workers.run_worker import build_client
    from src.workers.schedule import ensure_monitor_schedules

    settings = get_settings()
    loader = CollectionsLoader(path=settings.collections_file)
    collections = [c for c in loader.enabled()] or [default_collection()]

    activity.logger.info(
        f"Syncing schedules for {len(collections)} collections (signature {signature[:12]})"
    )

    await TemporalAdminService().resync_schedules(collections)

    client = await build_client()
    await ensure_monitor_schedules(client)
    activity.logger.info("monitor_schedules_ensured")

    repo = SyncStateRepository()
    await repo.set(COLLECTIONS_SIGNATURE_KEY, signature)
    await repo.set(
        COLLECTIONS_NAMES_KEY,
        json.dumps(sorted(c.name for c in collections)),
    )
    activity.logger.info("collections_signature_stored")


@activity.defn
async def collections_changes_activity() -> dict[str, list[str]]:
    """Diff the current enabled collection names against the stored snapshot.

    Runs BEFORE ``sync_collections_schedules_activity`` so the stored names
    still describe the previous registration. On the very first sync (no
    snapshot) both lists are empty to avoid a bootstrap alert.

    Returns:
        Dict with 'added' and 'removed' collection name lists.
    """
    from src.core.collections import CollectionsLoader, default_collection

    settings = get_settings()
    loader = CollectionsLoader(path=settings.collections_file)
    try:
        loader.load()
    except Exception as exc:
        activity.logger.error(f"collections_diff_parse_failed: {exc}")
        return {"added": [], "removed": []}

    current = sorted(c.name for c in loader.enabled()) or [default_collection().name]
    snapshot = await SyncStateRepository().get(COLLECTIONS_NAMES_KEY)
    if snapshot is None:
        return {"added": [], "removed": []}
    try:
        previous = set(json.loads(snapshot))
    except (TypeError, ValueError):
        previous = set()
    current_set = set(current)
    return {
        "added": sorted(current_set - previous),
        "removed": sorted(previous - current_set),
    }


@activity.defn
async def get_monitor_state_activity(key: str) -> str | None:
    """Read a monitor state key from the KV store.

    Args:
        key: KV key (e.g. 'monitor:last_summary_day').

    Returns:
        The stored value, or None.
    """
    return await SyncStateRepository().get(key)


@activity.defn
async def set_monitor_state_activity(key: str, value: str) -> None:
    """Write a monitor state key to the KV store.

    Args:
        key: KV key to write.
        value: Value to store.
    """
    await SyncStateRepository().set(key, value)


@activity.defn
async def run_monitor_checks_activity(config: dict[str, Any]) -> dict[str, Any]:
    """Run every registered system check and produce a MonitorReport.

    Persists each outcome to SQLite and the rotated JSONL log, prunes events
    beyond their retention window, and decides which checks need an alert
    (status transitions to error/warning). When ``alert_on_run`` is enabled it
    also appends an 'info' notification per newly completed digest run.

    Args:
        config: MonitorRunConfig fields (daily_summary, alert_on_run).

    Returns:
        The MonitorReport dict with ts, checks, and alert lists.
    """
    from src.api.models.monitoring import CheckResult, MonitorReport, MonitorRunConfig
    from src.api.services.monitoring.checks import run_all_checks
    from src.api.services.monitoring.reporter import MonitorReporter

    run_config = MonitorRunConfig(**config)
    settings = get_settings()

    checks = await run_all_checks()
    ts = datetime.now(UTC).isoformat()
    report = MonitorReport(ts=ts, checks=checks)

    reporter = MonitorReporter()
    await reporter.monitor_repo.purge_before(settings.monitor_retention_days)
    await reporter.persist(report)
    alerts = await reporter.decide_alerts(report)
    await reporter.record_last_statuses(report)

    if run_config.alert_on_run:
        for line in await reporter.new_completed_runs():
            alerts.append(
                CheckResult(check="digest_runs", status="info", detail=f"run completed: {line}")
            )
    report.alert = alerts
    activity.logger.info(
        f"monitor_run_finished checks={len(report.checks)} alerts={len(report.alert)}"
    )
    return report.model_dump()


@activity.defn
async def send_monitor_alert_activity(text: str) -> dict[str, Any]:
    """Deliver an alert through the configured monitor channels.

    Channels come from ``monitor_notify_channels`` (telegram, email, whatsapp).
    Each channel is attempted independently; a failure on one never aborts the
    others. An unconfigured channel is recorded, not raised.

    Args:
        text: The alert message body.

    Returns:
        Dict with status and per-channel results.
    """
    settings = get_settings()
    channels = settings.monitor_notify_channels or []
    results: list[dict[str, Any]] = []
    for channel in channels:
        if channel not in ("telegram", "email", "whatsapp"):
            results.append({"channel": channel, "status": "skipped", "reason": "unknown channel"})
            continue
        try:
            if channel == "telegram":
                chat_id = settings.monitor_telegram_chat_id or settings.telegram_chat_id
                outcome = await deliver_telegram_message_activity(text, chat_id)
                results.append({"channel": "telegram", "status": "ok", **outcome})
            elif channel == "email":
                recipients = settings.monitor_email_recipients
                if not recipients:
                    results.append(
                        {"channel": "email", "status": "skipped", "reason": "no recipients"}
                    )
                    continue
                outcome = await EmailService().send_digest_email(
                    recipients,
                    text,
                    "system-monitor",
                    subject="News-engine system alert",
                )
                results.append({"channel": "email", "status": "ok", **outcome})
            else:
                outcome = await WhatsAppService().send_text(
                    text,
                    settings.monitor_whatsapp_recipients,
                    group_id=settings.monitor_whatsapp_group_id,
                )
                results.append({"channel": "whatsapp", "status": "ok", **outcome})
        except Exception as exc:
            results.append({"channel": channel, "status": "error", "error": str(exc)})

    if not results:
        return {"status": "skipped", "reason": "no channels configured"}
    failed = [r for r in results if r.get("status") == "error"]
    return {
        "status": "error" if failed else "ok",
        "results": results,
    }
