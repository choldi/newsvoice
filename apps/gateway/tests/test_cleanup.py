"""Tests for cleanup task."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

import pytest

from tts_api.core.cleanup import (
    cleanup_expired_outputs,
    start_cleanup_task,
    stop_cleanup_task,
)


@pytest.mark.asyncio
async def test_cleanup_removes_expired_files(tmp_path: Path) -> None:
    """Test that expired files are removed."""
    # Create old file (2 hours ago)
    old_file = tmp_path / "old.wav"
    old_file.write_bytes(b"old data")
    old_time = time.time() - 7200
    os.utime(old_file, (old_time, old_time))

    # Create recent file
    recent_file = tmp_path / "recent.wav"
    recent_file.write_bytes(b"recent data")

    # Run cleanup with 0 interval (one-shot)
    task = asyncio.create_task(
        cleanup_expired_outputs(tmp_path, ttl_seconds=3600, interval_seconds=0)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert not old_file.exists()
    assert recent_file.exists()


@pytest.mark.asyncio
async def test_cleanup_ignores_non_wav_files(tmp_path: Path) -> None:
    """Test that non-.wav files are not deleted."""
    # Create old non-wav file
    old_file = tmp_path / "old.txt"
    old_file.write_bytes(b"old data")
    old_time = time.time() - 7200
    os.utime(old_file, (old_time, old_time))

    task = asyncio.create_task(
        cleanup_expired_outputs(tmp_path, ttl_seconds=3600, interval_seconds=0)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert old_file.exists()


@pytest.mark.asyncio
async def test_cleanup_handles_nonexistent_dir(tmp_path: Path) -> None:
    """Test that cleanup handles non-existent directory gracefully."""
    nonexistent = tmp_path / "nonexistent"
    # Should not raise
    task = asyncio.create_task(
        cleanup_expired_outputs(nonexistent, ttl_seconds=3600, interval_seconds=0)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cleanup_exits_on_cancel(tmp_path: Path) -> None:
    """Test that cleanup exits cleanly on CancelledError."""
    task = asyncio.create_task(
        cleanup_expired_outputs(tmp_path, ttl_seconds=3600, interval_seconds=10)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


@pytest.mark.asyncio
async def test_start_stop_cleanup_task(tmp_path: Path) -> None:
    """Test start and stop cleanup task lifecycle."""
    import tts_api.core.cleanup as mod

    mod._background_task = None

    start_cleanup_task(tmp_path, ttl_seconds=60)
    await asyncio.sleep(0.01)
    assert mod._background_task is not None
    assert not mod._background_task.done()

    stop_cleanup_task()
    await asyncio.sleep(0.01)
    assert mod._background_task.cancelled() or mod._background_task.done()


@pytest.mark.asyncio
async def test_start_cleanup_task_idempotent(tmp_path: Path) -> None:
    """Test that starting cleanup twice does not create a second task."""
    import tts_api.core.cleanup as mod

    mod._background_task = None

    start_cleanup_task(tmp_path, ttl_seconds=60)
    await asyncio.sleep(0.01)
    first_task = mod._background_task

    start_cleanup_task(tmp_path, ttl_seconds=60)
    assert mod._background_task is first_task

    stop_cleanup_task()
