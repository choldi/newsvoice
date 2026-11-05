"""Unit tests for the disk housekeeping activities (CleanupWorkflow).

The activity deletes every file older than ``cleanup_max_age_days`` under the
configured dirs, collapses the empty dirs that remain, and reports per-dir
counts. The settings are the single source of truth, so the tests patch them.
"""

import os
import time

import pytest
from temporalio.testing import ActivityEnvironment

from src.core.config import settings
from src.workers.cleanup_activities import cleanup_files_activity


@pytest.fixture
def tree(tmp_path: "os.PathLike", monkeypatch: pytest.MonkeyPatch) -> "os.PathLike":
    """Build a data tree with stale and fresh artifacts and patch settings."""
    root = tmp_path / "root"
    data = root / "data"
    tmp_dir = data / "audiobooks" / "tmp"
    gen_dir = data / "audiobooks" / "audio"
    (tmp_dir / "chunks").mkdir(parents=True)
    gen_dir.mkdir()

    stale = time.time() - 10 * 86400
    fresh = time.time() - 60

    stale_files = [
        tmp_dir / "ba9ddb236f87_merged.mp3",
        tmp_dir / "chunks" / "ch0001.wav",
        tmp_dir / "chunks" / "ch0002.wav",
        gen_dir / "old_book.mp3",
    ]
    fresh_files = [tmp_dir / "current_render.wav", gen_dir / "new_book.mp3"]
    for path in stale_files:
        path.write_bytes(b"x" * 8)
        os.utime(path, (stale, stale))
    for path in fresh_files:
        path.write_bytes(b"y" * 8)
        os.utime(path, (fresh, fresh))

    monkeypatch.chdir(root)
    monkeypatch.setattr(settings, "cleanup_max_age_days", 7)
    monkeypatch.setattr(settings, "cleanup_temp_dirs", ["data/audiobooks/tmp"])
    monkeypatch.setattr(settings, "cleanup_generation_dirs", ["data/audiobooks/audio"])
    return root


async def test_removes_stale_files_and_keeps_recent(tree: "os.PathLike") -> None:
    """Stale artifacts vanish, recent renders survive."""
    report = await ActivityEnvironment().run(cleanup_files_activity)

    assert report["status"] == "ok"
    assert not (tree / "data/audiobooks/tmp/ba9ddb236f87_merged.mp3").exists()
    assert not (tree / "data/audiobooks/tmp/chunks/ch0001.wav").exists()
    assert not (tree / "data/audiobooks/audio/old_book.mp3").exists()
    assert (tree / "data/audiobooks/tmp/current_render.wav").exists()
    assert (tree / "data/audiobooks/audio/new_book.mp3").exists()
    assert report["removed_files"] == 4
    assert report["removed_dirs"] == 1  # the now-empty chunks/ dir


async def test_collapses_empty_dirs_left_behind(tree: "os.PathLike") -> None:
    """Deletions leave behind empty subdirectories, not clutter."""

    await ActivityEnvironment().run(cleanup_files_activity)

    assert not (tree / "data/audiobooks/tmp/chunks").exists()
    assert (tree / "data/audiobooks/tmp").exists()  # the target dir itself stays


async def test_disabled_when_max_age_zero(
    tree: "os.PathLike", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero retention window is a documented no-op."""
    monkeypatch.setattr(settings, "cleanup_max_age_days", 0)

    report = await ActivityEnvironment().run(cleanup_files_activity)

    assert report["status"] == "disabled"
    assert (tree / "data/audiobooks/tmp/ba9ddb236f87_merged.mp3").exists()


async def test_missing_dir_is_reported(
    tree: "os.PathLike", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured dir that does not exist is skipped, not crashed on."""
    monkeypatch.setattr(settings, "cleanup_temp_dirs", ["data/audiobooks/tmp", "data/tts_cache"])

    report = await ActivityEnvironment().run(cleanup_files_activity)

    assert report["missing"] == ["data/tts_cache"]
    assert report["status"] == "ok"


async def test_symlinks_are_never_deleted(
    tree: "os.PathLike", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink (even stale) is skipped so external targets stay intact."""
    kept = tree / "data/audiobooks/tmp/current_render.wav"
    link = tree / "data/audiobooks/tmp/link.wav"
    old = time.time() - 10 * 86400
    os.symlink(kept, link)
    os.utime(link, (old, old), follow_symlinks=False)

    await ActivityEnvironment().run(cleanup_files_activity)

    assert link.is_symlink()  # skipped
    assert kept.exists()  # its referenced file intact


async def test_reports_per_dir_counts(tree: "os.PathLike") -> None:
    """The report carries per-dir numbers for the operator's log review."""

    report = await ActivityEnvironment().run(cleanup_files_activity)

    per_dir = report["per_dir"]
    assert per_dir["data/audiobooks/tmp"]["files"] == 3
    assert per_dir["data/audiobooks/tmp"]["dirs"] == 1
    assert per_dir["data/audiobooks/audio"]["files"] == 1
    assert report["removed_bytes_mb"] == round(4 * 8 / (1024 * 1024), 2)
