"""Temporal activities wrapping the news-engine services."""

import asyncio
from typing import Any

import feedparser
import httpx
from temporalio import activity

from src.api.models.article import ArticleIn, DigestIn
from src.api.models.cluster import ClusterIn
from src.api.models.tts import TTSIn
from src.api.services.cluster import ClusterService
from src.api.services.dedup import DedupService
from src.api.services.digest import DigestService
from src.api.services.tts import TTSService
from src.core.config import get_settings

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"


@activity.defn
async def fetch_rss_activity(feed_urls: list[str]) -> list[dict[str, Any]]:
    """Fetch and normalize articles from a list of RSS feeds.

    Args:
        feed_urls: List of RSS feed URLs to parse.

    Returns:
        List of article dicts compatible with the ArticleIn schema.
    """
    activity.logger.info("Fetching RSS feeds", feeds=len(feed_urls))
    articles: list[dict[str, Any]] = []
    for url in feed_urls:
        feed = await asyncio.to_thread(feedparser.parse, url)
        feed_obj = getattr(feed, "feed", None)
        source = getattr(feed_obj, "title", None) or url
        for entry in feed.entries:
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
    activity.logger.info("RSS fetch completed", articles=len(articles))
    return articles


@activity.defn
async def filter_new_activity(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out articles already delivered within the retention window.

    Args:
        articles: List of article dicts to deduplicate.

    Returns:
        List of new unseen article dicts.
    """
    activity.logger.info("Filtering new articles", count=len(articles))
    validated = [ArticleIn.model_validate(a) for a in articles]
    new_articles = await DedupService().filter_new_articles(validated)
    return [a.model_dump() for a in new_articles]


@activity.defn
async def cluster_activity(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group articles into event clusters.

    Args:
        articles: List of article dicts to cluster.

    Returns:
        List of cluster dicts conforming to the ClusterItem schema.
    """
    activity.logger.info("Clustering articles", count=len(articles))
    validated = [ArticleIn.model_validate(a) for a in articles]
    clusters = await ClusterService().cluster_articles(ClusterIn(articles=validated))
    return [c.model_dump() for c in clusters]


@activity.defn
async def generate_digest_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate the AI news digest from RSS articles/clusters.

    Args:
        payload: Dict matching the DigestIn schema (articles and/or clusters).

    Returns:
        Dict with status, digest markdown, and article_count.
    """
    activity.logger.info("Generating digest")
    digest_in = DigestIn.model_validate(payload)
    result = await DigestService().generate_digest(digest_in)
    return result.model_dump()


@activity.defn
async def generate_tts_activity(digest: dict[str, Any]) -> dict[str, Any]:
    """Convert the digest markdown into MP3 audio.

    Args:
        digest: Dict produced by generate_digest_activity (must contain 'digest').

    Returns:
        Dict with audio_path, duration_estimate, cached, voice, and char_count.
    """
    activity.logger.info("Generating TTS audio")
    tts_in = TTSIn(markdown=digest["digest"])
    result = await TTSService().generate_tts(tts_in)
    return result.model_dump()


async def _telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call the Telegram Bot API and normalize the response.

    Args:
        method: Telegram API method name (e.g. sendMessage, sendAudio).
        payload: JSON body for the request.

    Returns:
        Dict with status and Telegram message id.

    Raises:
        RuntimeError: If Telegram credentials are missing or the API errors.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Telegram bot token and chat id must be configured")
    url = TELEGRAM_API_URL.format(token=settings.telegram_bot_token, method=method)
    body = dict(payload)
    body["chat_id"] = settings.telegram_chat_id
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return {"status": "ok", "message_id": data["result"]["message_id"]}


@activity.defn
async def deliver_telegram_message_activity(text: str) -> dict[str, Any]:
    """Send the digest text via Telegram.

    Args:
        text: The digest markdown text to send.

    Returns:
        Dict with status and Telegram message id.
    """
    activity.logger.info("Sending Telegram message")
    return await _telegram_request("sendMessage", {"text": text, "parse_mode": "Markdown"})


@activity.defn
async def deliver_telegram_audio_activity(
    audio_path: str, caption: str | None = None
) -> dict[str, Any]:
    """Send a generated audio file via Telegram.

    Args:
        audio_path: Path to the MP3 file on disk.
        caption: Optional caption text for the audio message.

    Returns:
        Dict with status and Telegram message id.
    """
    activity.logger.info("Sending Telegram audio", audio_path=audio_path)
    with open(audio_path, "rb") as audio_file:
        files = {"audio": (audio_path.rsplit("/", 1)[-1], audio_file, "audio/mpeg")}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                TELEGRAM_API_URL.format(
                    token=get_settings().telegram_bot_token, method="sendAudio"
                ),
                data={"chat_id": get_settings().telegram_chat_id, "caption": caption},
                files=files,
            )
            response.raise_for_status()
            data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return {"status": "ok", "message_id": data["result"]["message_id"]}
