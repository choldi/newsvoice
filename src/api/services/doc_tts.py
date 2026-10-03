"""Long-form document TTS (Phase 7.11): chunk, synthesize, join into one MP3.

The digest TTS truncates to ``tts_max_chars`` because a digest is bounded; a
document is not. This service splits the speech text at paragraph/sentence
boundaries, synthesizes the chunks concurrently with Edge neural voices and
joins the MP3 frames in order, so a 300-page book becomes one playable file.

Every chunk is cached by its own content hash, so an interrupted run resumes
where it stopped instead of re-paying for the whole book.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from src.api.services.audio_files import atomic_write, read_sidecar, sidecar_path
from src.api.services.tts import estimate_duration_seconds, resolve_edge_voice, synthesize_mp3
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

# Schema version of the audio sidecar, so a future change of the hashing inputs
# invalidates cached files instead of serving audio built from other text.
SIDECAR_VERSION = 1

# Transient failures (Edge flaking on one chunk of a long book) are retried
# here rather than restarting the whole activity.
CHUNK_ATTEMPTS = 3

ProgressCallback = Callable[[int, int], None]


def _hash(*parts: str) -> str:
    """Hash the inputs that fully determine one synthesis.

    Args:
        *parts: Ordered inputs (voice, language, text...).

    Returns:
        Hex SHA-256 digest.
    """
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def slugify(title: str) -> str:
    """Turn a document title into a filesystem-safe stem.

    Args:
        title: Human title of the document.

    Returns:
        Lowercase [a-z0-9-] slug, 'documento' when nothing survives.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:40].strip("-") or "documento"


def chunk_speech_text(text: str, max_chars: int) -> list[str]:
    """Split speech text into chunks of at most ``max_chars`` characters.

    Boundaries are preferred in this order: paragraph, sentence, word, then a
    hard cut (only reachable when a single token exceeds the limit). No word is
    ever lost: joining the result with whitespace reproduces the input's words.

    Args:
        text: Plain speech text.
        max_chars: Maximum chunk length (>= 1).

    Returns:
        Non-empty chunks in reading order; [] for blank input.

    Raises:
        ValueError: When max_chars is below 1.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    remaining = (text or "").strip()
    if not remaining:
        return []

    chunks: list[str] = []
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        window = remaining[:max_chars]
        cut = window.rfind("\n\n")
        if cut <= 0:
            # Last sentence that still fits (the separator belongs to the cut).
            cut = _last_sentence_break(window)
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_chars  # a single token longer than the limit
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()
    return chunks


def _last_sentence_break(window: str) -> int:
    """Return the end offset of the last sentence inside ``window``.

    Args:
        window: Text prefix that fits in one chunk.

    Returns:
        Offset after the sentence terminator plus its trailing space, or -1.
    """
    best = -1
    for match in re.finditer(r"[.!?…][\"')\]]?\s", window):
        best = match.end()
    return best


@dataclass(frozen=True)
class DocTTSResult:
    """Outcome of one document synthesis."""

    audio_path: str
    voice: str
    language: str
    char_count: int
    chunk_count: int
    cached: bool
    duration_estimate: float
    title: str


class DocTTSService:
    """Synthesize a full document into a single cached MP3."""

    def __init__(self, base_dir: str | None = None) -> None:
        """Initialize the service with its storage layout.

        Args:
            base_dir: Root directory for chunk cache and finished audio.
                Defaults to ``settings.doc_tts_dir``.
        """
        settings = get_settings()
        self._base = Path(base_dir or settings.doc_tts_dir)
        self._chunk_dir = self._base / "chunks"
        self._audio_dir = self._base / "audio"
        self._chunk_chars = settings.doc_tts_chunk_chars
        self._concurrency = settings.doc_tts_concurrency
        self._request_timeout = settings.tts_request_timeout

    async def synthesize_document(
        self,
        *,
        text: str,
        voice: str = "",
        language: str = "",
        title: str = "",
        on_progress: ProgressCallback | None = None,
    ) -> DocTTSResult:
        """Synthesize the whole document, reusing cached chunks and files.

        Args:
            text: Speech-ready plain text (already markdown-stripped).
            voice: Requested voice (Edge ShortName or legacy alias); empty uses
                the configured default.
            language: ISO 639-1 language; empty uses the configured default.
            title: Document title, only used to name the output file.
            on_progress: Called after every finished chunk with
                ``(done, total)``; used for activity heartbeats.

        Returns:
            The finished MP3 path with its metadata.

        Raises:
            RuntimeError: When a chunk keeps failing after the retries, or a
                synthesis yields no audio.
        """
        settings = get_settings()
        language = language or settings.tts_language
        resolved_voice = await resolve_edge_voice(voice or settings.tts_voice, language)
        speech_text = text.strip()
        if not speech_text:
            raise RuntimeError("No text to synthesize")

        digest = _hash(resolved_voice, language, speech_text)
        audio_path = self._audio_dir / f"doc_{slugify(title)}_{digest[:12]}.mp3"
        sidecar = sidecar_path(audio_path)
        self._audio_dir.mkdir(parents=True, exist_ok=True)

        existing = read_sidecar(sidecar) if audio_path.exists() else None
        chunks = chunk_speech_text(speech_text, self._chunk_chars)
        if existing and existing.get("sha256") == digest:
            logger.info(
                "doc_tts_cached",
                audio_path=str(audio_path),
                chunk_count=existing.get("chunk_count"),
            )
            return DocTTSResult(
                audio_path=str(audio_path),
                voice=resolved_voice,
                language=language,
                char_count=len(speech_text),
                chunk_count=int(existing.get("chunk_count") or len(chunks)),
                cached=True,
                duration_estimate=estimate_duration_seconds(len(speech_text)),
                title=title,
            )

        self._chunk_dir.mkdir(parents=True, exist_ok=True)
        total = len(chunks)
        done = 0
        semaphore = asyncio.Semaphore(self._concurrency)

        async def _one(index: int) -> bytes:
            nonlocal done
            chunk = chunks[index]
            chunk_digest = _hash(resolved_voice, language, chunk)[:16]
            chunk_path = self._chunk_dir / f"{chunk_digest}.mp3"
            if not chunk_path.exists():
                async with semaphore:
                    data = await _synthesize_with_retries(
                        chunk, resolved_voice, timeout=self._request_timeout
                    )
                atomic_write(chunk_path, data)
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            return chunk_path.read_bytes()

        parts = await asyncio.gather(*(_one(i) for i in range(total)))
        audio_bytes = b"".join(parts)
        atomic_write(audio_path, audio_bytes)
        atomic_write(
            sidecar,
            json.dumps(
                {
                    "version": SIDECAR_VERSION,
                    "sha256": digest,
                    "voice": resolved_voice,
                    "language": language,
                    "title": title,
                    "char_count": len(speech_text),
                    "chunk_count": total,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                sort_keys=True,
            ).encode(),
        )
        logger.info(
            "doc_tts_generated",
            audio_path=str(audio_path),
            voice=resolved_voice,
            char_count=len(speech_text),
            chunk_count=total,
        )
        return DocTTSResult(
            audio_path=str(audio_path),
            voice=resolved_voice,
            language=language,
            char_count=len(speech_text),
            chunk_count=total,
            cached=False,
            duration_estimate=estimate_duration_seconds(len(speech_text)),
            title=title,
        )


async def _synthesize_with_retries(text: str, voice: str, *, timeout: int) -> bytes:
    """Synthesize one chunk, retrying transient Edge failures.

    Args:
        text: Chunk text.
        voice: Resolved Edge voice ShortName.
        timeout: Per-attempt timeout in seconds.

    Returns:
        MP3 bytes of the chunk.

    Raises:
        RuntimeError: When every attempt failed.
    """
    last_error: Exception | None = None
    for attempt in range(1, CHUNK_ATTEMPTS + 1):
        try:
            return await synthesize_mp3(text, voice, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised after the attempts
            last_error = exc
            if attempt < CHUNK_ATTEMPTS:
                logger.warning(
                    "doc_tts_chunk_retry",
                    attempt=attempt,
                    voice=voice,
                    error=str(exc),
                )
                await asyncio.sleep(float(attempt))
    raise RuntimeError(f"chunk synthesis failed after {CHUNK_ATTEMPTS} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Job metadata (title/voice/char counts) kept next to the audio, because the
# audio_jobs table only tracks status and cannot answer "which document is
# this?" without a second read.
# ---------------------------------------------------------------------------

# Workflow ids are ours ("doc-tts-<hex>"); they become file paths, so anything
# outside this alphabet is rejected before it reaches the filesystem.
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def validate_job_id(workflow_id: str) -> str:
    """Validate a document job id before it is used in a filesystem path.

    Args:
        workflow_id: Workflow id of the job.

    Returns:
        The validated id.

    Raises:
        ValueError: When the id is empty or contains foreign characters.
    """
    if not workflow_id or not _JOB_ID_RE.fullmatch(workflow_id):
        raise ValueError(f"invalid document job id: {workflow_id!r}")
    return workflow_id


def job_text_path(workflow_id: str, base_dir: str | None = None) -> Path:
    """Return the path of the extracted text file of one document job.

    Args:
        workflow_id: Workflow id of the job.
        base_dir: Root directory; defaults to ``settings.doc_tts_dir``.

    Returns:
        Path like ``<doc_tts_dir>/text/<workflow_id>.txt``.

    Raises:
        ValueError: When the id is not one of ours (blocks path traversal).
    """
    validate_job_id(workflow_id)
    base = Path(base_dir or get_settings().doc_tts_dir)
    return base / "text" / f"{workflow_id}.txt"


def job_meta_path(workflow_id: str, base_dir: str | None = None) -> Path:
    """Return the metadata JSON path of one document job.

    Args:
        workflow_id: Workflow id of the job.
        base_dir: Root directory; defaults to ``settings.doc_tts_dir``.

    Returns:
        Path like ``<doc_tts_dir>/meta/<workflow_id>.json``.

    Raises:
        ValueError: When the id is not one of ours (blocks path traversal).
    """
    validate_job_id(workflow_id)
    base = Path(base_dir or get_settings().doc_tts_dir)
    return base / "meta" / f"{workflow_id}.json"


def read_job_meta(workflow_id: str, base_dir: str | None = None) -> dict:
    """Read a job's metadata, tolerating a missing or corrupted file.

    Args:
        workflow_id: Workflow id of the job.
        base_dir: Root directory; defaults to ``settings.doc_tts_dir``.

    Returns:
        The stored metadata, or an empty dict when absent.
    """
    try:
        path = job_meta_path(workflow_id, base_dir)
    except ValueError:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_job_meta(workflow_id: str, meta: dict, base_dir: str | None = None) -> None:
    """Write a job's metadata atomically, merging with what is already there.

    Args:
        workflow_id: Workflow id of the job.
        meta: Fields to store; keys already present are overwritten.
        base_dir: Root directory; defaults to ``settings.doc_tts_dir``.

    Raises:
        ValueError: When the id is not one of ours.
    """
    path = job_meta_path(workflow_id, base_dir)
    merged = {**read_job_meta(workflow_id, base_dir), **meta}
    atomic_write(path, json.dumps(merged, indent=2, sort_keys=True).encode())


def update_job_meta(workflow_id: str, **fields: object) -> None:
    """Alias of :func:`write_job_meta` for incremental updates from activities.

    Args:
        workflow_id: Workflow id of the job.
        **fields: Fields to merge into the stored metadata.
    """
    write_job_meta(workflow_id, dict(fields))
