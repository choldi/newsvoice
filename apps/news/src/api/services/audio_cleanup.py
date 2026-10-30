"""Purging of finished audio jobs from the tools page (Phase 9.8).

The tools page lists generated audio (documents, lessons, narrations) and lets
the operator delete a run. One audio file must not leave garbage behind: the
row, its audio and sidecar, the stored upload, the extracted text and meta and,
for lessons, the spoken script. Narrations additionally clear the run's
``narration_path`` so the collections page stops pointing at a deleted file.

All paths are confined: run ids are validated with the kind's own validator
before they become filenames, and the stored ``audio_path`` is only removed
when it resolves inside its kind's directory.
"""

from __future__ import annotations

from pathlib import Path

from src.api.services.audio_files import sidecar_path
from src.api.services.doc_tts import validate_job_id
from src.api.services.lesson import validate_lesson_id
from src.core.config import get_settings


def _resolved_audio(path_str: str, roots: list[Path]) -> Path | None:
    """Resolve a stored audio path and confine it to its directories.

    The path comes from our own DB row, but no row should ever be able to make
    the GUI delete a foreign file: resolve it and require it to live under one
    of the kind's audio roots.

    Args:
        path_str: Stored ``audio_path`` value.
        roots: Only files under these directories may be removed.

    Returns:
        The resolved path when it is inside one root, else None.
    """
    try:
        resolved = Path(path_str).resolve()
    except OSError:
        return None
    for root in roots:
        if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
            return resolved
    return None


def delete_job_artifacts(run_id: str, kind: str, audio_path: str = "") -> list[str]:
    """Delete everything a finished audio job produced from disk.

    Confined file removal: the run id is validated with the kind's own
    validator before it keys any filename, and the stored ``audio_path`` (the
    one name not derived from the run id, e.g. a narration's hashed cache
    file) is only removed when it resolves inside the kind's audio directory.

    Args:
        run_id: Workflow id of the run (row key with ``kind``).
        kind: Audio job kind: 'doc_tts', 'lesson', 'narration' or
            'audiobook'.
        audio_path: Stored audio path of the finished job ('' keeps the
            well-known glob-only purge, used by the tests).

    Returns:
        Removed paths (possibly empty when nothing was on disk).

    Raises:
        ValueError: When the ``run_id`` is not valid for the kind (foreign
            characters would become unsafe filenames).
    """
    settings = get_settings()
    if kind == "doc_tts":
        validate_job_id(run_id)
        root = Path(settings.doc_tts_dir)
        audio_roots = [root / "audio", root]
    elif kind == "lesson":
        validate_lesson_id(run_id)
        root = Path(settings.teacher_dir)
        audio_roots = [Path(settings.narration_cache_dir), root]
    elif kind == "narration":
        validate_job_id(run_id)
        root = Path(settings.narration_cache_dir)
        audio_roots = [root]
    elif kind == "audiobook":
        validate_job_id(run_id)
        root = Path(settings.audiobook_dir)
        audio_roots = [root / "audio", root]
    else:
        raise ValueError(f"unknown audio job kind: {kind!r}")

    removed: list[str] = []
    audio = _resolved_audio(audio_path, audio_roots) if audio_path else None
    if audio is not None:
        audio.unlink(missing_ok=True)
        removed.append(str(audio))
        sidecar = sidecar_path(audio)
        if sidecar.exists():
            sidecar.unlink()
            removed.append(str(sidecar))

    folders = [root / subdir for subdir in ("uploads", "text", "meta")]
    if kind == "audiobook":
        # Audiobooks keep the audio and the cover under audiobook_dir, but
        # reuse the document-TTS upload/meta directories. Covers and the old
        # tmp cover naming are keyed by the run id too.
        folders.extend(
            [
                root / "covers",
                root / "tmp",
                Path(settings.doc_tts_dir) / "uploads",
                Path(settings.doc_tts_dir) / "meta",
            ]
        )
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.glob(f"{run_id}.*"):
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed
