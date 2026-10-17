"""Unit tests for the ffmpeg-based audio compression service."""

import os
from pathlib import Path

import pytest

from src.api.services.audio_compress import AudioCompressService, CompressAudioError


class _RunResult:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _make_older_than(path: Path, other: Path) -> None:
    """Force the mtime of path to be one second older than other.

    Args:
        path: File whose mtime is rewritten.
        other: File providing the reference timestamp.
    """
    stamp = other.stat().st_mtime - 1
    os.utime(path, (stamp, stamp))


async def test_compress_builds_ffmpeg_command_and_returns_m4a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compress() runs ffmpeg with AAC mono args and renames the .m4a into place."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF fake wav")
    target = source.with_suffix(".m4a")
    tmp_target = source.with_name("digest.tmp.m4a")
    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        Path(command[-1]).write_bytes(b"\x00\x00m4a fake")
        return _RunResult()

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fake_run)
    service = AudioCompressService(ffmpeg_path="ffmpeg", bitrate_kbps=48)

    result = await service.compress(str(source))

    assert result == str(target)
    assert target.read_bytes() == b"\x00\x00m4a fake"
    assert not tmp_target.exists()
    expected_args = [
        "-i",
        str(source),
        "-ac",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "48k",
        "-movflags",
        "+faststart",
        str(tmp_target),
    ]
    assert all(arg in calls[0] for arg in expected_args)


async def test_compress_regenerates_when_target_is_older_than_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source newer than the cached M4A forces a new transcode."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF fake wav")
    target = source.with_suffix(".m4a")
    target.write_bytes(b"\x00 stale")
    _make_older_than(target, source)
    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        Path(command[-1]).write_bytes(b"\x00 fresh")
        return _RunResult()

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fake_run)

    result = await AudioCompressService().compress(str(source))

    assert len(calls) == 1
    assert result == str(target)
    assert target.read_bytes() == b"\x00 fresh"


async def test_compress_leaves_no_partial_target_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed transcode keeps the previous M4A and removes the temp file."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF fake wav")
    target = source.with_suffix(".m4a")
    target.write_bytes(b"\x00 previous")
    _make_older_than(target, source)

    def fake_run(command, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        Path(command[-1]).write_bytes(b"\x00 partial")
        return _RunResult(returncode=1, stderr="Invalid data found")

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fake_run)
    service = AudioCompressService()

    with pytest.raises(CompressAudioError, match="Invalid data found"):
        await service.compress(str(source))

    assert target.read_bytes() == b"\x00 previous"
    assert not source.with_name("digest.tmp.m4a").exists()


async def test_compress_reuses_existing_m4a_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compress() skips ffmpeg when the target M4A already exists."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF fake wav")
    target = source.with_suffix(".m4a")
    target.write_bytes(b"\x00 existing")

    called = False

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("ffmpeg should not be invoked")

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fail_run)

    result = await AudioCompressService().compress(str(source))

    assert result == str(target)
    assert called is False


async def test_compress_missing_source_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compress() rejects a non-existent source file."""

    def fail_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("ffmpeg should not be invoked")

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fail_run)

    with pytest.raises(CompressAudioError, match="not found"):
        await AudioCompressService().compress(str(tmp_path / "missing.wav"))


async def test_compress_failed_ffmpeg_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compress() surfaces the ffmpeg stderr on a non-zero exit code."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF broken")

    def fake_run(command, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _RunResult(returncode=1, stderr="Invalid data found")

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", fake_run)

    with pytest.raises(CompressAudioError, match="Invalid data found"):
        await AudioCompressService().compress(str(source))


async def test_compress_missing_ffmpeg_binary_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """compress() raises when the ffmpeg executable cannot be found."""
    source = tmp_path / "digest.wav"
    source.write_bytes(b"RIFF broken")

    def missing_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr("src.api.services.audio_compress.subprocess.run", missing_run)

    with pytest.raises(CompressAudioError, match="ffmpeg not found"):
        await AudioCompressService().compress(str(source))
