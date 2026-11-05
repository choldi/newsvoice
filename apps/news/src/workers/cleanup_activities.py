"""Disk housekeeping activities: delete stale temp files and old audio.

The ``disk-cleanup`` schedule owns the cadence; these activities own the
deletion rules. Everything they may remove must be a plain file (or the empty
directory a deletion leaves behind) under one of the configured dirs, so a bad
path can never take the database or the model cache with it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity

from src.core.config import settings

logger = structlog.get_logger(__name__)

SECONDS_PER_DAY = 86400
DELETE_BATCH = 64

__all__ = ["cleanup_files_activity"]


def _resolve_dir(raw: str) -> Path | None:
    """Resolve one configured dir like every other settings path.

    Relative dirs (``data/audiobooks/tmp``) resolve against the CWD exactly
    like ``settings.audiobook_dir`` or ``settings.tts_cache_dir`` do; absolute
    paths are used as-is.

    Args:
        raw: Entry of ``cleanup_temp_dirs`` / ``cleanup_generation_dirs``.

    Returns:
        The directory path, or ``None`` when the entry is empty.
    """
    path = Path((raw or "").strip())
    return path if str(path) else None


def collect_stale(path: Path, cutoff: float) -> tuple[list[Path], list[Path]]:
    """Split a tree into stale files and stale empty directories.

    Args:
        path: Directory to inspect.
        cutoff: Files with ``mtime`` before this are considered stale.

    Returns:
        ``(files, dirs)``: stale files in walk order and directories whose
        whole content is gone (the caller deletes them bottom-up).
    """

    def _walk(current: Path, for_files: list[Path], for_dirs: list[Path]) -> None:
        try:
            entries = sorted(
                p for p in current.iterdir() if not p.is_symlink() and p.name != ".gitkeep"
            )
        except OSError as exc:
            logger.warning("cleanup_scan_failed", path=str(current), error=str(exc))
            return
        for entry in entries:
            if entry.is_dir():
                _walk(entry, for_files, for_dirs)
                for_dirs.append(entry)
                continue
            try:
                stale = not entry.is_file() or entry.stat().st_mtime < cutoff
            except OSError as exc:
                logger.warning("cleanup_stat_failed", path=str(entry), error=str(exc))
                continue
            if stale:
                for_files.append(entry)

    files: list[Path] = []
    dirs: list[Path] = []
    _walk(path, files, dirs)
    return files, dirs


def _prune_tree(path: Path, cutoff: float, report: dict[str, Any]) -> int:
    """Delete stale files and the empty dirs they leave under ``path``.

    Args:
        path: Directory being cleaned.
        cutoff: Modified-before timestamp of the deletable files.
        report: Aggregate report mutated in place (bytes/files/errors).

    Returns:
        The number of directory entries the caller may drop next.
    """
    files, dirs = collect_stale(path, cutoff)
    for batch_index in range(0, len(files), DELETE_BATCH):
        for entry in files[batch_index : batch_index + DELETE_BATCH]:
            try:
                size = entry.stat().st_size
                entry.unlink()
                report["removed_files"] += 1
                report["removed_bytes"] += size
                _bump(report, path, size=size)
            except FileNotFoundError:
                continue
            except IsADirectoryError:
                continue
            except OSError as exc:
                logger.warning("cleanup_delete_failed", path=str(entry), error=str(exc))
                report["errors"].append(f"{entry}: {exc}")
        activity.heartbeat(f"deleted={report['removed_files']}")
    for entry in reversed(dirs):
        try:
            entry.rmdir()  # never deletes real content: only empty dirs survive
            report["removed_dirs"] += 1
            _bump(report, path, is_dir=True)
        except OSError:
            continue  # still has content or failed; leave it to the next run
    return len(files)


def _bump(report: dict[str, Any], path: Path, *, size: int = 0, is_dir: bool = False) -> None:
    key = "dirs" if is_dir else "files"
    stats = report["per_dir"].setdefault(str(path), {"files": 0, "bytes": 0, "dirs": 0})
    stats[key] += 1
    stats["bytes"] += size


@activity.defn
async def cleanup_files_activity(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Delete files older than the configured age in every cleanup dir.

    The schedule payload is ignored on purpose: the values come from the
    ``cleanup_max_age_days`` settings on each run, so the .env knob applies
    without touching the schedule.

    Args:
        config: Unused; Kept open for future per-run overrides.

    Returns:
        Report with per-dir counts, totals and any deletion errors.
    """
    max_age_days = int(settings.cleanup_max_age_days)
    report: dict[str, Any] = {
        "status": "ok",
        "max_age_days": max_age_days,
        "removed_files": 0,
        "removed_bytes": 0,
        "removed_dirs": 0,
        "per_dir": {},
        "errors": [],
    }
    if max_age_days <= 0:
        report["status"] = "disabled"
        logger.info("cleanup_disabled")
        return report
    cutoff = time.time() - max_age_days * SECONDS_PER_DAY
    for raw in list(settings.cleanup_temp_dirs) + list(settings.cleanup_generation_dirs):
        path = _resolve_dir(raw)
        if path is None:
            continue
        if not path.is_dir():
            report.setdefault("missing", []).append(str(path))
            continue
        before = report["removed_files"]
        _prune_tree(path, cutoff, report)
        cleaned = report["removed_files"] - before
        logger.info("cleanup_dir_done", path=str(path), removed_files=cleaned)
    report["removed_bytes_mb"] = round(report["removed_bytes"] / (1024 * 1024), 2)
    if report["errors"]:
        report["status"] = "partial"
    logger.info(
        "cleanup_run_done",
        removed_files=report["removed_files"],
        removed_bytes_mb=report["removed_bytes_mb"],
    )
    return report
