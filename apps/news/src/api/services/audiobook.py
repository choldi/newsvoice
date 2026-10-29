"""Audiobook M4B pipeline (Fase B2)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.api.models.narration import TTSBackend
from src.api.services.audio_files import atomic_write
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

AUDIOBOOK_KIND = "audiobook"
AUDIOBOOK_COLLECTION = "audiobooks"


@dataclass(frozen=True)
class AudiobookChapter:
    title: str
    text: str
    start_index: int = 0


@dataclass(frozen=True)
class AudiobookResult:
    m4b_path: str
    chapters: int
    duration_seconds: float
    cached: bool = False


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:60].strip("-") or "audiobook"


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


def split_chapters(text: str, max_chars: int = 8000) -> list[AudiobookChapter]:
    if not text.strip():
        return []
    # try headings
    m = re.split(r"(?m)^#{1,3}\s+.+$", text)
    if len(m) >= 2:
        blocks = [b.strip() for b in m if b.strip()]
        if len(blocks) >= 2:
            chapters = []
            for i, b in enumerate(blocks):
                chapters.append(AudiobookChapter(title=f"Capítulo {i + 1}", text=b))
            return chapters
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) >= 2 and max_chars >= 8000:
        chapters = []
        for i, b in enumerate(blocks):
            chapters.append(AudiobookChapter(title=f"Capítulo {i + 1}", text=b))
        return chapters
    chapters = []
    buf: list[str] = []
    total = 0
    for b in blocks:
        if total + len(b) > max_chars and buf:
            chapters.append(
                AudiobookChapter(title=f"Capítulo {len(chapters) + 1}", text="\n\n".join(buf))
            )
            buf, total = [], 0
        buf.append(b)
        total += len(b)
    if buf:
        chapters.append(
            AudiobookChapter(title=f"Capítulo {len(chapters) + 1}", text="\n\n".join(buf))
        )
    return chapters


class AudiobookService:
    def __init__(self, base_dir: str | None = None) -> None:
        settings = get_settings()
        self._base = Path(base_dir or getattr(settings, "audiobook_dir", "data/audiobooks"))
        self._audio_dir = self._base / "audio"
        self._tmp_dir = self._base / "tmp"
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    async def render_m4b(
        self,
        *,
        text: str,
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
    ) -> AudiobookResult:
        from src.api.services.doc_tts import DocTTSService

        chapters = split_chapters(text)
        if not chapters:
            raise RuntimeError("No chapters to render")
        settings = get_settings()
        language = language or settings.tts_language
        digest = _hash(title, language, str(backend), text, voice)
        m4b_path = self._audio_dir / f"{_slugify(title)}_{digest[:12]}.m4b"
        manifest_path = self._tmp_dir / f"audiobook_{digest[:12]}.manifest.json"

        if m4b_path.exists():
            return AudiobookResult(
                m4b_path=str(m4b_path), chapters=len(chapters), duration_seconds=0.0, cached=True
            )

        # Cover extraction if not provided
        if not cover_path and run_id:
            # try common upload paths
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
        }
        if resume and manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {"digest": digest, "chapters": [], "merged": None, "normed": None}

        service = DocTTSService(base_dir=str(self._tmp_dir / "doc_tts"))
        chapter_files: list[Path] = []
        chapter_durations: list[float] = []

        manifest_chapters = manifest.get("chapters") or []
        for idx, ch in enumerate(chapters):
            if gate is not None:
                try:
                    await gate.before_turn()
                except Exception:
                    pass
            done_info = manifest_chapters[idx] if idx < len(manifest_chapters) else {}
            cf_path = Path(
                done_info.get("path") or self._tmp_dir / f"{digest[:12]}_ch{idx:04d}.mp3"
            )
            if resume and cf_path.exists() and done_info.get("done"):
                chapter_files.append(cf_path)
                chapter_durations.append(
                    float(done_info.get("duration") or _probe_duration(cf_path))
                )
                continue
            res = await service.synthesize_document(
                text=ch.text,
                voice=voice,
                language=language,
                title=f"{title} - {ch.title}",
            )
            cf = Path(res.audio_path)
            # copy to stable tmp name
            stable = self._tmp_dir / f"{digest[:12]}_ch{idx:04d}.mp3"
            try:
                if cf != stable:
                    import shutil

                    shutil.copy2(cf, stable)
            except Exception:
                stable = cf
            chapter_files.append(stable)
            d = _probe_duration(stable)
            chapter_durations.append(d)
            if idx >= len(manifest_chapters):
                manifest_chapters.append({})
            manifest_chapters[idx] = {"path": str(stable), "done": True, "duration": d}
            manifest["chapters"] = manifest_chapters
            try:
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            except Exception:
                pass

        # reuse merged
        merged_cand = manifest.get("merged")
        if resume and merged_cand and Path(merged_cand).exists():
            merged = Path(merged_cand)
        else:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, dir=str(self._tmp_dir)
            ) as f:
                for cf in chapter_files:
                    f.write(f"file '{cf}'\n")
                concat_list = f.name
            merged = self._tmp_dir / f"{digest[:12]}_merged.mp3"
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
                    str(merged),
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
                        str(merged),
                    ]
                    subprocess.run(cmd, check=True, timeout=3600)
            finally:
                try:
                    os.unlink(concat_list)
                except Exception:
                    pass
            manifest["merged"] = str(merged)
            try:
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            except Exception:
                pass

        if loudnorm:
            normed_cand = manifest.get("normed")
            if resume and normed_cand and Path(normed_cand).exists():
                merged = Path(normed_cand)
            else:
                normed = self._tmp_dir / f"{digest[:12]}_norm.mp3"
                try:
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(merged),
                        "-af",
                        "loudnorm=I=-18:TP=-1.5:LRA=11",
                        "-ar",
                        "44100",
                        str(normed),
                    ]
                    subprocess.run(cmd, check=True, timeout=3600)
                    merged = normed
                    manifest["normed"] = str(normed)
                    try:
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    except Exception:
                        pass
                except Exception as exc:
                    logger.warning("audiobook_loudnorm_failed", error=str(exc))

        meta_file = self._tmp_dir / f"{digest[:12]}.ffmeta"
        with meta_file.open("w", encoding="utf-8") as mf:
            mf.write(";FFMETADATA1\n")
            if title:
                mf.write(f"title={title}\n")
            if author:
                mf.write(f"artist={author}\n")
            pos = 0.0
            for idx, ch in enumerate(chapters):
                dur = chapter_durations[idx] if idx < len(chapter_durations) else 60.0
                if dur <= 0:
                    dur = 60.0
                start_ms = int(pos * 1000)
                end_ms = int((pos + dur) * 1000)
                mf.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_ms}\nEND={end_ms}\n")
                mf.write(f"title={ch.title}\n")
                pos += dur

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
                str(m4b_path),
            ]
            if cover_path and Path(cover_path).exists():
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(merged),
                    "-i",
                    str(cover_path),
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
                    str(m4b_path),
                ]
            try:
                subprocess.run(cmd, check=True, timeout=7200)
            except Exception:
                # fallback re-encode audio if streams incompatible
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(merged),
                    "-i",
                    str(cover_path) if cover_path and Path(cover_path).exists() else str(meta_file),
                ]
                if cover_path and Path(cover_path).exists():
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
                        "copy" if cover_path and Path(cover_path).exists() else "none",
                        "-f",
                        "mp4",
                        str(m4b_path),
                    ]
                )
                subprocess.run(cmd, check=True, timeout=7200)
        except Exception as exc:
            logger.error("audiobook_mux_failed", error=str(exc))
            raise

        total_dur = sum(chapter_durations) if chapter_durations else _probe_duration(m4b_path)
        return AudiobookResult(
            m4b_path=str(m4b_path),
            chapters=len(chapters),
            duration_seconds=round(total_dur, 2),
            cached=False,
        )


async def submit_audiobook(
    upload,
    content_length: str,
    voice: str | None = None,
    language: str | None = None,
    title: str | None = None,
):
    from pathlib import Path

    from src.api.db.database import AudioJobsRepository
    from src.api.services.doc_tts import (
        JOB_COLLECTION,
        DocumentExtractionError,
        SubmitError,
        _uploads_dir,
        atomic_write,
        read_upload,
        sniff_kind,
        write_job_meta,
    )
    from src.core.config import get_settings
    from src.workers.audiobook_workflows import AudiobookConfig

    settings = get_settings()

    # The render runs through DocTTSService (edge); reject another engine's
    # vocabulary before storing any data (same rule as document TTS).
    requested_voice = (voice or "").strip()
    if requested_voice:
        from src.api.services.voice_engine import VoiceEngineError, assert_voice_engine

        try:
            assert_voice_engine(requested_voice, TTSBackend.EDGE, field="voice")
        except VoiceEngineError as exc:
            raise SubmitError(
                f"audiobook only accepts edge voices: {exc}", status_code=422
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
    clean_title = (title or "").strip() or Path(filename).stem
    write_job_meta(
        workflow_id,
        {
            "title": clean_title,
            "filename": filename,
            "kind": kind,
            "voice": (voice or "").strip(),
            "language": (language or "").strip(),
        },
    )

    repo = AudioJobsRepository()
    await repo.start_job(workflow_id, collection=JOB_COLLECTION, kind=AUDIOBOOK_KIND)
    try:
        from src.api.services.temporal_admin import TemporalAdminService

        await TemporalAdminService().start_audiobook(
            AudiobookConfig(
                source_path=str(upload_path),
                filename=filename,
                title=clean_title,
                voice=(voice or "").strip(),
                language=(language or "").strip(),
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
