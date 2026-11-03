"""Audiobook M4B pipeline (Fase B2)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.api.models.narration import TTSBackend
from src.api.services.audio_files import atomic_write
from src.api.services.book_structure import (
    PROFILE_VERSION,
    BookBlock,
    BookRole,
    DeliveryProfile,
    blocks_from_markdown,
    blocks_from_plain_text,
    chapters_from_blocks,
    profile_for,
)
from src.core.audio import (
    CANONICAL_FORMAT,
    AudioError,
    build_wav,
    concat_wavs,
    silence_bytes,
    wav_format,
)
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

AUDIOBOOK_KIND = "audiobook"
AUDIOBOOK_COLLECTION = "audiobooks"


@dataclass(frozen=True)
class AudiobookChapter:
    title: str
    text: str
    start_index: int = 0
    blocks: tuple[BookBlock, ...] = ()


@dataclass(frozen=True)
class AudiobookResult:
    m4b_path: str
    chapters: int
    duration_seconds: float
    cached: bool = False
    digest: str = ""
    chapter_start: int = 1
    chapter_end: int = 0


#: Callback receiving partial progress dicts while a render advances.
ProgressCallback = Callable[[dict[str, Any]], Any]


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:60].strip("-") or "audiobook"


def digest_prefix_from_audio_path(audio_path: str | None) -> str:
    """Recover the 12-char render digest from a finished M4B filename.

    The job metadata stores the digest for runs started after the partial
    download feature, but the older runs only have ``audio_path``. Their names
    follow ``{slug}_{digest12}.m4b`` (or ``..._ch0001-0010.m4b``), so the
    digest can still be recovered for a partial download.

    Args:
        audio_path: Value of ``audio_jobs.audio_path``.

    Returns:
        The 12-char digest prefix, or an empty string when it cannot be read.
    """
    if not audio_path:
        return ""
    match = re.search(r"_([0-9a-f]{12})(?:_ch\d{4}-\d{4})?\.m4b$", Path(audio_path).name)
    return match.group(1) if match else ""


def _hash(*parts: str) -> str:
    import hashlib

    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        val = result.stdout.decode().strip()
        return float(val)
    except Exception:
        return 0.0


def extract_cover_from_epub(epub_path: Path) -> Path | None:
    try:
        with zipfile.ZipFile(epub_path, "r") as z:
            names = z.namelist()
            candidates = [
                n
                for n in names
                if re.search(r"cover", n, re.I)
                and n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            ]
            for c in candidates:
                data = z.read(c)
                out = (
                    epub_path.parent / f"cover_{_hash(c)[:10]}.{Path(c).suffix.lower().lstrip('.')}"
                )
                if out.suffix.lower() == ".webp":
                    # try to convert if possible
                    tmp = out.with_suffix(".jpg")
                    atomic_write(tmp, data)
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-y",
                                "-i",
                                str(tmp),
                                str(out),
                            ],
                            check=True,
                            timeout=30,
                        )
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        return tmp if tmp.exists() else None
                    return out if out.exists() else None
                atomic_write(out, data)
                return out
    except Exception as exc:
        logger.debug("epub_cover_extract_failed", error=str(exc))
    return None


def _blocks_from_text(text: str, title: str = "") -> list[BookBlock]:
    """Parse speech text into blocks, detecting Markdown headings when present."""
    if re.search(r"(?m)^#{1,6}\s+\S", text or ""):
        return blocks_from_markdown(text, title)
    return blocks_from_plain_text(text, title)


def _chapters_from_book_blocks(
    blocks: list[BookBlock], max_chars: int = 8000
) -> list[AudiobookChapter]:
    """Convert structural chapters into the renderer's chapter shape."""
    return [
        AudiobookChapter(title=chapter.title, text=chapter.text, blocks=chapter.blocks)
        for chapter in chapters_from_blocks(blocks, max_chars=max_chars)
    ]


def split_chapters(text: str, max_chars: int = 8000) -> list[AudiobookChapter]:
    """Split text into structural chapters, preserving heading titles.

    Args:
        text: Speech-ready text, optionally containing Markdown headings.
        max_chars: Maximum size of a chapter without an explicit heading.

    Returns:
        Chapters in reading order; empty for blank input.
    """
    if not text.strip():
        return []
    return _chapters_from_book_blocks(_blocks_from_text(text), max_chars=max_chars)


def _chapter_progress(
    *,
    done: int,
    total: int,
    chapter: int,
    title: str,
    elapsed: float,
    audio_seconds: float,
    chunks_done: int = 0,
    chunks_total: int = 0,
) -> dict[str, Any]:
    """Build the progress payload written to the job metadata.

    Args:
        done: Chapters of the selected range already rendered.
        total: Chapters in the selected range.
        chapter: 1-based global chapter number being rendered.
        title: Chapter title shown by the GUI.
        elapsed: Wall-clock seconds since the render started.
        audio_seconds: Audio length of the chapters already rendered.
        chunks_done: Chunks of the current chapter already synthesized.
        chunks_total: Chunks the current chapter is split into.

    Returns:
        JSON-serializable progress dict.
    """
    return {
        "done": done,
        "total": total,
        "chapter": chapter,
        "chapter_title": title,
        "elapsed_seconds": elapsed,
        "audio_seconds": audio_seconds,
        "chunks_done": chunks_done,
        "chunks_total": chunks_total,
    }


def _concat_audio_files(files: list[Path], out_path: Path, tmp_dir: Path) -> Path:
    """Join chapter audio files into one file, re-encoding only if needed.

    Args:
        files: Chapter files in playback order.
        out_path: Destination file (extension decides the container).
        tmp_dir: Directory for the ffmpeg concat list.

    Returns:
        ``out_path``.

    Raises:
        RuntimeError: When there is no audio to join.
    """
    if not files:
        raise RuntimeError("no chapter audio to join")
    if len(files) == 1:
        if files[0] != out_path:
            if files[0].suffix.lower() == out_path.suffix.lower():
                atomic_write(out_path, files[0].read_bytes())
            else:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(files[0]),
                        "-ar",
                        "44100",
                        "-ac",
                        "2",
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        "192k",
                        str(out_path),
                    ],
                    check=True,
                    timeout=3600,
                )
        return out_path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, dir=str(tmp_dir)) as f:
        for cf in files:
            f.write(f"file '{Path(cf).resolve()}'\n")
        concat_list = f.name
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list,
            "-c",
            "copy",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=3600)
        except Exception:
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list,
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(out_path),
            ]
            subprocess.run(cmd, check=True, timeout=3600)
    finally:
        try:
            os.unlink(concat_list)
        except OSError:
            pass
    return out_path


def _apply_loudnorm(src: Path, out_path: Path) -> Path:
    """Normalize loudness to -18 LUFS / TP -1.5 into ``out_path``.

    Args:
        src: Merged audio file.
        out_path: Normalized destination.

    Returns:
        ``out_path``.

    Raises:
        subprocess.CalledProcessError: When ffmpeg fails; callers fall back to
            the unnormalized file.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-af",
        "loudnorm=I=-18:TP=-1.5:LRA=11",
        "-ar",
        "44100",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, timeout=3600)
    return out_path


def _write_ffmeta(
    path: Path,
    *,
    title: str,
    author: str,
    chapter_titles: list[str],
    durations: list[float],
) -> None:
    """Write the ffmpeg chapter/metadata sidecar for an M4B.

    Args:
        path: Destination ``.ffmeta`` file.
        title: Book title.
        author: Book author (stored as ``artist``).
        chapter_titles: Chapter titles in playback order.
        durations: Chapter durations in seconds (missing/zero defaults to 60).
    """
    with path.open("w", encoding="utf-8") as mf:
        mf.write(";FFMETADATA1\n")
        if title:
            mf.write(f"title={title}\n")
        if author:
            mf.write(f"artist={author}\n")
        pos = 0.0
        for idx, chapter_title in enumerate(chapter_titles):
            dur = durations[idx] if idx < len(durations) else 60.0
            if dur <= 0:
                dur = 60.0
            start_ms = int(pos * 1000)
            end_ms = int((pos + dur) * 1000)
            mf.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_ms}\nEND={end_ms}\n")
            mf.write(f"title={chapter_title}\n")
            pos += dur


def _mux_m4b(
    *,
    merged: Path,
    meta_file: Path,
    cover_path: str | None,
    out_path: Path,
) -> None:
    """Mux audio, chapters and optional cover into an M4B, atomically.

    The output is written to a sibling temp file and published with
    ``os.replace``: a failed ffmpeg run must never leave a partial ``.m4b``
    that the next request would mistake for a finished (cached) render.

    Args:
        merged: Normalized/merged audio file.
        meta_file: ``.ffmeta`` chapter/metadata sidecar.
        cover_path: Optional cover image.
        out_path: Final M4B path.

    Raises:
        Exception: Whatever ffmpeg failed with, after cleaning up the temp file.
    """
    cover = cover_path if cover_path and Path(cover_path).exists() else None
    tmp_m4b = out_path.with_name(f".{out_path.stem}.{os.getpid()}.part.m4b")
    tmp_m4b.unlink(missing_ok=True)
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(merged),
            "-i",
            str(meta_file),
            "-map_metadata",
            "1",
            "-c",
            "copy",
            "-f",
            "mp4",
            str(tmp_m4b),
        ]
        if cover:
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(merged),
                "-i",
                str(cover),
                "-i",
                str(meta_file),
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-map_metadata",
                "2",
                "-c:a",
                "copy",
                "-c:v",
                "copy",
                "-disposition:v:0",
                "attached_pic",
                "-f",
                "mp4",
                str(tmp_m4b),
            ]
        try:
            subprocess.run(cmd, check=True, timeout=7200)
        except Exception:
            # Fallback: re-encode the audio when the streams are incompatible.
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(merged),
                "-i",
                str(cover) if cover else str(meta_file),
            ]
            if cover:
                cmd.extend(
                    ["-i", str(meta_file), "-map", "0:a", "-map", "1:v", "-map_metadata", "2"]
                )
            else:
                cmd.extend(["-map_metadata", "1"])
            cmd.extend(
                [
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "44100",
                    "-c:v",
                    "copy" if cover else "none",
                    "-f",
                    "mp4",
                    str(tmp_m4b),
                ]
            )
            subprocess.run(cmd, check=True, timeout=7200)
        os.replace(tmp_m4b, out_path)
    except Exception as exc:
        tmp_m4b.unlink(missing_ok=True)
        logger.error("audiobook_mux_failed", error=str(exc))
        raise


class AudiobookService:
    def __init__(self, base_dir: str | None = None) -> None:
        settings = get_settings()
        self._base = Path(base_dir or getattr(settings, "audiobook_dir", "data/audiobooks"))
        self._audio_dir = self._base / "audio"
        self._tmp_dir = self._base / "tmp"
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._concurrency = settings.doc_tts_concurrency

    async def render_m4b(
        self,
        *,
        text: str,
        blocks: list[BookBlock] | tuple[BookBlock, ...] | None = None,
        title: str = "",
        author: str = "",
        cover_path: str | None = None,
        voice: str = "",
        language: str = "es",
        backend: TTSBackend = TTSBackend.EDGE,
        loudnorm: bool = True,
        gate: Any = None,
        run_id: str | None = None,
        resume: bool = True,
        chapter_range: tuple[int, int] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AudiobookResult:
        structured = [
            block if isinstance(block, BookBlock) else BookBlock.from_dict(block)
            for block in (blocks or [])
        ]
        all_chapters = (
            _chapters_from_book_blocks(structured) if structured else split_chapters(text)
        )
        if not all_chapters:
            raise RuntimeError("No chapters to render")
        total_chapters = len(all_chapters)

        if chapter_range is not None:
            requested_start, requested_end = chapter_range
            start = max(1, int(requested_start or 1))
            end = int(requested_end or total_chapters)
        else:
            start, end = 1, total_chapters
        end = min(end, total_chapters)
        if start > total_chapters or start > end:
            raise RuntimeError(
                f"chapter range {start}-{end} is outside this book (1-{total_chapters})"
            )
        chapters = all_chapters[start - 1 : end]
        ranged = start != 1 or end != total_chapters

        settings = get_settings()
        language = language or settings.tts_language

        # The uploaded cover is resolved before the cache key: choosing a new
        # cover (or author/loudnorm) must produce a new M4B instead of serving
        # the previous one.
        if not cover_path and run_id:
            covers_dir = self._base / "covers"
            if covers_dir.is_dir():
                for cover_file in sorted(covers_dir.glob(f"{run_id}.*")):
                    if cover_file.is_file():
                        cover_path = str(cover_file)
                        break

        block_signature = "\x1e".join(
            f"{block.role}:{block.title}:{block.text}" for block in structured
        )
        digest = _hash(
            title,
            author,
            language,
            str(backend),
            text,
            block_signature,
            voice,
            cover_path or "",
            "1" if loudnorm else "0",
            PROFILE_VERSION,
        )
        # A range render never overwrites the full-book artifacts: its output
        # and intermediate names carry the slice, while the chapter files stay
        # digest-keyed so the chunks and chapters are shared with a full render.
        suffix = f"_ch{start:04d}-{end:04d}" if ranged else ""
        m4b_path = self._audio_dir / f"{_slugify(title)}_{digest[:12]}{suffix}.m4b"
        manifest_path = self._tmp_dir / f"audiobook_{digest[:12]}.manifest.json"
        range_key = f"{start}-{end}"
        started = time.monotonic()

        async def _emit(payload: dict[str, Any]) -> None:
            if on_progress is None:
                return
            outcome = on_progress(payload)
            if inspect.isawaitable(outcome):
                await outcome

        await _emit(
            {
                "digest": digest,
                "chapters_total": total_chapters,
                "chapter_start": start,
                "chapter_end": end,
                "progress": {
                    "done": 0,
                    "total": len(chapters),
                    "elapsed_seconds": 0.0,
                    "chapter_start": start,
                    "chapter_end": end,
                },
            }
        )

        if m4b_path.exists():
            duration = _probe_duration(m4b_path)
            if duration > 0:
                await _emit(
                    {
                        "cached": True,
                        "progress": {
                            "done": len(chapters),
                            "total": len(chapters),
                            "elapsed_seconds": round(time.monotonic() - started, 2),
                            "audio_seconds": round(duration, 2),
                            "chapter_start": start,
                            "chapter_end": end,
                        },
                    }
                )
                return AudiobookResult(
                    m4b_path=str(m4b_path),
                    chapters=len(chapters),
                    duration_seconds=round(duration, 2),
                    cached=True,
                    digest=digest,
                    chapter_start=start,
                    chapter_end=end,
                )

        # Fallback cover: extract it from the source EPUB only on a cache miss.
        if not cover_path and run_id:
            for cand in [
                Path("data/doc_tts/uploads") / f"{run_id}.epub",
                Path("data/doc_tts/uploads") / f"{run_id}.EPUB",
                Path("data/lessons/uploads") / f"{run_id}.epub",
                Path("data/lessons/uploads") / f"{run_id}.EPUB",
            ]:
                if cand.exists():
                    cov = extract_cover_from_epub(cand)
                    if cov:
                        cover_path = str(cov)
                    break

        manifest: dict[str, Any] = {
            "digest": digest,
            "chapters": [],
            "merged": None,
            "normed": None,
            "ranges": {},
        }
        if resume and manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest.update(loaded)
            except Exception:
                pass
        manifest.setdefault("digest", digest)
        manifest.setdefault("chapters", [])
        if not isinstance(manifest.get("chapters"), list):
            manifest["chapters"] = []
        if not isinstance(manifest.get("ranges"), dict):
            manifest["ranges"] = {}
        range_state = manifest["ranges"].setdefault(range_key, {})

        def _save_manifest() -> None:
            try:
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            except Exception:
                pass

        chapter_files: list[Path] = []
        chapter_durations: list[float] = []
        chapter_ext = ".wav"

        manifest_chapters = manifest["chapters"]
        completed = 0
        audio_seconds = 0.0

        for offset, ch in enumerate(chapters):
            idx = start - 1 + offset
            if gate is not None and await gate.should_pause():
                await gate.yield_and_wait()
            done_info = manifest_chapters[idx] if idx < len(manifest_chapters) else {}
            cf_path = Path(
                done_info.get("path") or self._tmp_dir / f"{digest[:12]}_ch{idx:04d}{chapter_ext}"
            )
            if resume and cf_path.exists() and done_info.get("done"):
                chapter_files.append(cf_path)
                d = float(done_info.get("duration") or _probe_duration(cf_path))
                chapter_durations.append(d)
                audio_seconds += d
                completed += 1
                await _emit(
                    {
                        "progress": _chapter_progress(
                            done=completed,
                            total=len(chapters),
                            chapter=idx + 1,
                            title=ch.title,
                            elapsed=round(time.monotonic() - started, 2),
                            audio_seconds=round(audio_seconds, 2),
                        )
                    }
                )
                continue
            stable = self._tmp_dir / f"{digest[:12]}_ch{idx:04d}{chapter_ext}"

            async def _chunk_progress(
                done_chunks: int,
                total_chunks: int,
                *,
                _idx: int = idx,
                _title: str = ch.title,
            ) -> None:
                await _emit(
                    {
                        "progress": _chapter_progress(
                            done=completed,
                            total=len(chapters),
                            chapter=_idx + 1,
                            title=_title,
                            elapsed=round(time.monotonic() - started, 2),
                            audio_seconds=round(audio_seconds, 2),
                            chunks_done=done_chunks,
                            chunks_total=total_chunks,
                        )
                    }
                )

            await self._synthesize_chapter(
                text=ch.text,
                blocks=ch.blocks,
                out_path=stable,
                voice=voice,
                language=language,
                backend=backend,
                gate=gate,
                on_progress=_chunk_progress,
            )
            chapter_files.append(stable)
            d = _probe_duration(stable)
            chapter_durations.append(d)
            audio_seconds += d
            completed += 1
            if idx >= len(manifest_chapters):
                manifest_chapters.extend({} for _ in range(idx - len(manifest_chapters) + 1))
            manifest_chapters[idx] = {"path": str(stable), "done": True, "duration": d}
            manifest["chapters"] = manifest_chapters
            _save_manifest()
            await _emit(
                {
                    "progress": _chapter_progress(
                        done=completed,
                        total=len(chapters),
                        chapter=idx + 1,
                        title=ch.title,
                        elapsed=round(time.monotonic() - started, 2),
                        audio_seconds=round(audio_seconds, 2),
                    )
                }
            )

        # Reuse the merged/normalized files of this exact range. A range keeps
        # its intermediates under ``ranges`` so it can never overwrite the
        # full-book ones; the full render keeps the legacy top-level keys.
        merged_cand = range_state.get("merged") if ranged else manifest.get("merged")
        merged = self._tmp_dir / f"{digest[:12]}{suffix}_merged.mp3"
        if resume and merged_cand and Path(merged_cand).exists():
            merged = Path(merged_cand)
        else:
            _concat_audio_files(chapter_files, merged, self._tmp_dir)
            if ranged:
                range_state["merged"] = str(merged)
            else:
                manifest["merged"] = str(merged)
            _save_manifest()

        if loudnorm:
            normed_cand = range_state.get("normed") if ranged else manifest.get("normed")
            normed = self._tmp_dir / f"{digest[:12]}{suffix}_norm.mp3"
            if resume and normed_cand and Path(normed_cand).exists():
                merged = Path(normed_cand)
            else:
                try:
                    _apply_loudnorm(merged, normed)
                    merged = normed
                    if ranged:
                        range_state["normed"] = str(normed)
                    else:
                        manifest["normed"] = str(normed)
                    _save_manifest()
                except Exception as exc:
                    logger.warning("audiobook_loudnorm_failed", error=str(exc))

        meta_file = self._tmp_dir / f"{m4b_path.stem}.ffmeta"
        _write_ffmeta(
            meta_file,
            title=title,
            author=author,
            chapter_titles=[ch.title for ch in chapters],
            durations=chapter_durations,
        )
        _mux_m4b(
            merged=merged,
            meta_file=meta_file,
            cover_path=cover_path,
            out_path=m4b_path,
        )
        if ranged:
            range_state["m4b"] = str(m4b_path)
        _save_manifest()

        total_dur = sum(chapter_durations) if chapter_durations else _probe_duration(m4b_path)
        return AudiobookResult(
            m4b_path=str(m4b_path),
            chapters=len(chapters),
            duration_seconds=round(total_dur, 2),
            cached=False,
            digest=digest,
            chapter_start=start,
            chapter_end=end,
        )

    def build_partial_m4b(
        self,
        *,
        digest_prefix: str,
        title: str,
        author: str = "",
        loudnorm: bool = True,
        up_to: int | None = None,
        run_id: str | None = None,
    ) -> tuple[Path, int]:
        """Mux the finished chapters of a render into a preview M4B.

        The full render writes each chapter to the digest-keyed manifest as
        soon as it is done, so this method can build a playable file from the
        middle of a multi-hour run without touching the final artifacts. The
        partial file is cached by digest, first/last chapter and loudness mode.

        Args:
            digest_prefix: First 12 hex chars of the render digest.
            title: Book title (used for the filename and M4B metadata).
            author: Book author.
            loudnorm: Apply the same loudnorm as the final render.
            up_to: Only include the first N finished chapters (None = all).
            run_id: Optional workflow id, used to pick up an uploaded cover.

        Returns:
            Tuple of (partial M4B path, number of chapters included).

        Raises:
            RuntimeError: When the digest/manifest is unavailable or no chapter
                has finished yet.
        """
        prefix = (digest_prefix or "")[:12].lower()
        if not re.fullmatch(r"[0-9a-f]{12}", prefix):
            raise RuntimeError("this run has no render digest yet")
        manifest_path = self._tmp_dir / f"audiobook_{prefix}.manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("the render manifest is not available yet")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"the render manifest is unreadable: {exc}") from exc
        entries = manifest.get("chapters") or []
        done: list[tuple[int, Path, float]] = []
        for idx, info in enumerate(entries):
            if not isinstance(info, dict) or not info.get("done"):
                continue
            path = Path(str(info.get("path") or ""))
            if not path.is_file():
                continue
            done.append((idx, path, float(info.get("duration") or _probe_duration(path))))
        if up_to is not None and up_to > 0:
            done = done[:up_to]
        if not done:
            raise RuntimeError("no finished chapters to build a partial audiobook from")

        first_idx, last_idx = done[0][0], done[-1][0]
        partial_dir = self._base / "partial"
        partial_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            partial_dir
            / f"{_slugify(title)}_{prefix}_partial{first_idx + 1:04d}-{last_idx + 1:04d}.m4b"
        )
        if out_path.exists() and _probe_duration(out_path) > 0:
            return out_path, len(done)

        cover_path: str | None = None
        if run_id:
            covers_dir = self._base / "covers"
            if covers_dir.is_dir():
                for cover_file in sorted(covers_dir.glob(f"{run_id}.*")):
                    if cover_file.is_file():
                        cover_path = str(cover_file)
                        break

        merged = (
            self._tmp_dir / f"{prefix}_partial{first_idx + 1:04d}-{last_idx + 1:04d}_merged.mp3"
        )
        _concat_audio_files([path for _, path, _ in done], merged, self._tmp_dir)
        if loudnorm:
            normed = merged.with_name(f"{merged.stem}_norm.mp3")
            try:
                _apply_loudnorm(merged, normed)
                merged = normed
            except Exception as exc:
                logger.warning("audiobook_partial_loudnorm_failed", error=str(exc))

        meta_file = self._tmp_dir / f"{out_path.stem}.ffmeta"
        _write_ffmeta(
            meta_file,
            title=title,
            author=author,
            chapter_titles=[f"Capítulo {idx + 1}" for idx, _, _ in done],
            durations=[duration for _, _, duration in done],
        )
        _mux_m4b(
            merged=merged,
            meta_file=meta_file,
            cover_path=cover_path,
            out_path=out_path,
        )
        return out_path, len(done)

    async def _synthesize_chapter(
        self,
        *,
        text: str,
        out_path: Path,
        voice: str,
        language: str,
        backend: TTSBackend,
        blocks: tuple[BookBlock, ...] | list[BookBlock] | None = None,
        gate: Any = None,
        on_progress: Callable[[int, int], Any] | None = None,
    ) -> Path:
        """Synthesize one chapter block by block and join it with pauses.

        Every structural block carries its own :class:`DeliveryProfile`, so the
        same chapter can mix a slow title, a calm preface and normal body
        prose. The blocks are synthesized independently and joined into one
        canonical WAV with the profile's paragraph and sentence pauses.

        Args:
            text: Chapter speech text (used when ``blocks`` is empty).
            out_path: Chapter audio file to write (canonical WAV).
            voice: Requested voice id (resolved through ``voice_for``).
            language: ISO 639-1 language code.
            backend: Engine that synthesizes (always the local gateway/Edge).
            blocks: Structural blocks in reading order.
            gate: Optional RenderGate for the digest hand-over.
            on_progress: Optional callback ``(done_chunks, total_chunks)``
                invoked after each chunk so the GUI can show intra-chapter
                progress. It may be sync or async.

        Returns:
            ``out_path`` once the chapter file exists.

        Raises:
            RuntimeError: When a synthesis yields no audio.
            TTSBackendError: When the engine has no usable voice here.
        """
        from src.api.services.doc_tts import chunk_speech_text
        from src.api.services.tts_backends import voice_for

        sequence = list(blocks or ())
        if not sequence:
            if not text.strip():
                raise RuntimeError("No text to synthesize")
            sequence = [BookBlock(role=BookRole.BODY, text=text)]

        # Audiobooks never render on Modal: qwen3_tts is pinned to the local
        # gateway engine (a book-length Modal render would cost real money).
        resolved = await voice_for(backend, voice, language, run_in_modal=False)
        chunk_dir = self._tmp_dir / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        plan: list[tuple[DeliveryProfile, list[tuple[str, Path, int]]]] = []
        total_chunks = 0
        for block in sequence:
            speech = block.text.strip()
            if not speech:
                continue
            chunks = chunk_speech_text(speech, 1500)
            if not chunks:
                continue
            profile = profile_for(block.role)
            entries: list[tuple[str, Path, int]] = []
            for index, chunk in enumerate(chunks):
                key = _hash(
                    str(backend),
                    resolved,
                    language,
                    PROFILE_VERSION,
                    str(block.role),
                    f"{profile.speed:.3f}",
                    f"{profile.pitch_hz:.3f}",
                    profile.emotion or "",
                    chunk,
                )[:16]
                pause = (
                    profile.sentence_pause_ms if index < len(chunks) - 1 else profile.pause_after_ms
                )
                entries.append((chunk, chunk_dir / f"{key}.wav", pause))
            plan.append((profile, entries))
            total_chunks += len(entries)

        if total_chunks == 0:
            raise RuntimeError("No text to synthesize")

        semaphore = asyncio.Semaphore(self._concurrency)
        progress_lock = asyncio.Lock()
        done_chunks = 0

        async def _prepare(profile: DeliveryProfile, chunk: str, chunk_path: Path) -> None:
            nonlocal done_chunks
            async with semaphore:
                if not chunk_path.exists():
                    if gate is not None and await gate.should_pause():
                        await gate.yield_and_wait()
                    data = await self._synthesize_canonical_chunk(
                        backend=backend,
                        text=chunk,
                        voice=resolved,
                        language=language,
                        profile=profile,
                    )
                    atomic_write(chunk_path, data)
            async with progress_lock:
                done_chunks += 1
                if on_progress is not None:
                    outcome = on_progress(done_chunks, total_chunks)
                    if inspect.isawaitable(outcome):
                        await outcome

        await asyncio.gather(
            *(
                _prepare(profile, chunk, chunk_path)
                for profile, entries in plan
                for chunk, chunk_path, _pause in entries
            )
        )

        parts = [
            (chunk_path.read_bytes(), pause)
            for _profile, entries in plan
            for _chunk, chunk_path, pause in entries
        ]
        last_pause = parts[-1][1]
        if last_pause > 0:
            parts[-1] = (parts[-1][0], 0)
            parts.append((build_wav(silence_bytes(last_pause)), 0))
        atomic_write(out_path, concat_wavs(parts))
        return out_path

    async def _synthesize_canonical_chunk(
        self,
        *,
        backend: TTSBackend,
        text: str,
        voice: str,
        language: str,
        profile: DeliveryProfile,
    ) -> bytes:
        """Synthesize one chunk with its profile and return canonical WAV bytes.

        Args:
            backend: Engine to use.
            text: Chunk speech text.
            voice: Engine-specific resolved voice.
            language: ISO 639-1 language code.
            profile: Delivery profile for this structural block.

        Returns:
            Mono 16-bit PCM at the canonical narration rate.

        Raises:
            RuntimeError: When every attempt failed or the engine returned
                no audio.
        """
        from src.api.services.tts_backends import synthesize

        options: dict[str, Any] = {"run_in_modal": False}
        if profile.speed != 1.0:
            options["speed"] = profile.speed
        if profile.emotion:
            options["emotion"] = profile.emotion
        if profile.pitch_hz:
            options["pitch_hz"] = profile.pitch_hz

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                data = await synthesize(backend, text, voice, language, **options)
                if not data:
                    raise RuntimeError(f"{backend.value} synthesis produced no audio for {voice!r}")
                return self._canonical_wav(data)
            except Exception as exc:  # noqa: BLE001 - re-raised after the attempts
                last_error = exc
                if attempt < 3:
                    logger.warning(
                        "audiobook_chunk_retry",
                        attempt=attempt,
                        backend=str(backend),
                        error=str(exc),
                    )
                    await asyncio.sleep(float(attempt))
        raise RuntimeError(f"chunk synthesis failed after 3 attempts: {last_error}")

    def _canonical_wav(self, data: bytes) -> bytes:
        """Return ``data`` as canonical WAV, resampling with ffmpeg if needed.

        Args:
            data: Audio bytes in any format ffmpeg can read.

        Returns:
            Canonical WAV bytes.

        Raises:
            RuntimeError: When ffmpeg cannot convert the payload.
        """
        try:
            if wav_format(data) == CANONICAL_FORMAT:
                return data
        except AudioError:
            pass

        with tempfile.TemporaryDirectory(dir=str(self._tmp_dir)) as tmp:
            source = Path(tmp) / "input.audio"
            target = Path(tmp) / "canonical.wav"
            source.write_bytes(data)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-ac",
                str(CANONICAL_FORMAT.channels),
                "-ar",
                str(CANONICAL_FORMAT.framerate),
                "-c:a",
                "pcm_s16le",
                str(target),
            ]
            subprocess.run(cmd, check=True, timeout=600)
            if not target.exists():
                raise RuntimeError("ffmpeg produced no canonical WAV")
            return target.read_bytes()


def parse_chapter_range(
    chapter_start: str | int | None,
    chapter_end: str | int | None,
) -> tuple[int | None, int | None]:
    """Parse the optional 1-based inclusive chapter range from a form.

    Args:
        chapter_start: First chapter as submitted (string or int, may be empty).
        chapter_end: Last chapter as submitted (string or int, may be empty).

    Returns:
        ``(start, end)`` with None for "unset".

    Raises:
        SubmitError: 422 when a value is not a positive integer or the range is
            inverted.
    """
    from src.api.services.doc_tts import SubmitError

    def _one(value: str | int | None, label: str) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except (TypeError, ValueError) as exc:
            raise SubmitError(f"{label} debe ser un número entero", status_code=422) from exc
        if parsed < 1:
            raise SubmitError(f"{label} debe ser mayor o igual que 1", status_code=422)
        return parsed

    start = _one(chapter_start, "chapter_start")
    end = _one(chapter_end, "chapter_end")
    if start is not None and end is not None and start > end:
        raise SubmitError("chapter_start no puede ser mayor que chapter_end", status_code=422)
    return start, end


async def submit_audiobook(
    upload,
    content_length: str,
    voice: str | None = None,
    language: str | None = None,
    title: str | None = None,
    engine: str | None = None,
    cover=None,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
):
    from pathlib import Path

    from src.api.db.database import AudioJobsRepository
    from src.api.models.narration import TTSBackend
    from src.api.services.doc_tts import (
        DocumentExtractionError,
        SubmitError,
        _uploads_dir,
        atomic_write,
        read_upload,
        sniff_kind,
        write_job_meta,
    )
    from src.api.services.tts_backends import TTSBackendError, resolve_backend
    from src.api.services.voice_engine import VoiceEngineError, assert_voice_engine
    from src.core.config import get_settings
    from src.workers.audiobook_workflows import AudiobookConfig

    settings = get_settings()

    # The engine drives both the voice vocabulary and the synthesis path;
    # reject an unknown backend before storing any data.
    try:
        backend = resolve_backend(engine or "edge")
    except TTSBackendError as exc:
        raise SubmitError(str(exc), status_code=422) from exc

    # Modal is banned for audiobooks: a book renders for hours, so the two
    # Modal-only engines would spend real GPU money. qwen3_tts stays allowed
    # because the audiobook path always pins it to the local gateway engine.
    if backend in (TTSBackend.ZONOS2, TTSBackend.VOXCPM2):
        raise SubmitError(
            "audiobooks cannot run on Modal engines (zonos2/voxcpm2): "
            "pick edge, tts-api gateway, Kokoro or Qwen3-TTS (local gateway)",
            status_code=422,
        )

    if chapter_start is not None and chapter_start < 1:
        raise SubmitError("chapter_start debe ser mayor o igual que 1", status_code=422)
    if chapter_end is not None and chapter_end < 1:
        raise SubmitError("chapter_end debe ser mayor o igual que 1", status_code=422)
    if chapter_start is not None and chapter_end is not None and chapter_start > chapter_end:
        raise SubmitError("chapter_start no puede ser mayor que chapter_end", status_code=422)

    # Reject a voice outside the chosen engine's vocabulary before storing
    # data (same rule as document TTS, but against the selected engine).
    requested_voice = (voice or "").strip()
    if requested_voice:
        try:
            assert_voice_engine(requested_voice, backend, field="voice")
        except VoiceEngineError as exc:
            raise SubmitError(
                f"voice does not belong to engine {backend.value}: {exc}", status_code=422
            ) from exc

    filename = Path(upload.filename or "").name.strip()
    if not filename:
        raise SubmitError("file part has no filename", status_code=422)
    try:
        kind = sniff_kind(filename)
    except DocumentExtractionError as exc:
        raise SubmitError(str(exc), status_code=415) from exc
    data = await read_upload(upload, getattr(settings, "doc_tts_max_file_bytes", 20_000_000))

    from src.api.services.temporal_admin import new_audiobook_run_id

    workflow_id = new_audiobook_run_id()
    upload_path = _uploads_dir() / f"{workflow_id}{Path(filename).suffix.lower()}"
    atomic_write(upload_path, data)

    # Persist an explicit cover next to the run; render_m4b picks it up from
    # the covers directory (workflow-id keyed) or extracts it from the EPUB.
    if cover is not None and getattr(cover, "filename", None):
        try:
            cover_bytes = await cover.read()
            cover_ext = Path(str(cover.filename)).suffix.lower() or ".jpg"
            covers_dir = Path(getattr(settings, "audiobook_dir", "data/audiobooks")) / "covers"
            covers_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(covers_dir / f"{workflow_id}{cover_ext}", cover_bytes)
        except Exception as exc:
            logger.warning("audiobook_cover_save_failed", error=str(exc))

    clean_title = (title or "").strip() or Path(filename).stem
    write_job_meta(
        workflow_id,
        {
            "title": clean_title,
            "filename": filename,
            "kind": kind,
            "voice": (voice or "").strip(),
            "language": (language or "").strip(),
            "engine": str(backend),
            "mode": str(backend),
            "chapter_start": chapter_start,
            "chapter_end": chapter_end,
        },
    )

    repo = AudioJobsRepository()
    await repo.start_job(workflow_id, collection=AUDIOBOOK_COLLECTION, kind=AUDIOBOOK_KIND)
    await repo.set_chapter_range(
        workflow_id,
        chapter_start,
        chapter_end,
        kind=AUDIOBOOK_KIND,
    )
    try:
        from src.api.services.temporal_admin import TemporalAdminService

        await TemporalAdminService().start_audiobook(
            AudiobookConfig(
                source_path=str(upload_path),
                filename=filename,
                title=clean_title,
                voice=(voice or "").strip(),
                language=(language or "").strip(),
                engine=str(backend),
                chapter_start=chapter_start,
                chapter_end=chapter_end,
            ),
            workflow_id,
        )
    except Exception as exc:
        logger.error(
            "audiobook_start_failed", workflow_id=workflow_id, error=str(exc), exc_info=True
        )
        try:
            await repo.finish_job(
                workflow_id,
                status="FINISHED_ERROR",
                error=f"could not start the workflow: {exc}",
                kind=AUDIOBOOK_KIND,
            )
        except Exception:
            pass
        upload_path.unlink(missing_ok=True)
        raise SubmitError("Could not start the audiobook workflow", status_code=502) from exc

    logger.info("audiobook_submitted", workflow_id=workflow_id, filename=filename, kind=kind)
    return {
        "workflow_id": workflow_id,
        "title": clean_title,
        "filename": filename,
        "kind": kind,
        "status_url": f"/api/admin/audiobooks/{workflow_id}",
    }
