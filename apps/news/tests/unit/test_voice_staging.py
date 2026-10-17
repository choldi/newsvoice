"""Unit tests for the reference staging service (Phase 7.9)."""

import gzip
from typing import Any

import httpx
import pytest

from src.api.services import voice_staging
from src.api.services.voice_staging import VoiceStagingError
from src.core.audio import CANONICAL_FORMAT, build_wav
from src.core.config import settings

WAV = build_wav(b"\xff\x7f" * 2400, CANONICAL_FORMAT)


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stand-in for ``httpx.AsyncClient`` that records the staging POST."""

    calls: list[dict[str, Any]] = []
    status_code: int = 200
    text: str = "ok"
    error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _FakeResponse:
        if type(self).error is not None:
            raise type(self).error
        _FakeClient.calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResponse(_FakeClient.status_code, _FakeClient.text)


@pytest.fixture(autouse=True)
def _staging_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Write staged files into an isolated directory and fake the HTTP client."""
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "voice_staging_api_key", "staging-key")
    monkeypatch.setattr(settings, "voice_staging_url", "http://server.test:8000")
    _FakeClient.calls = []
    _FakeClient.status_code = 200
    _FakeClient.text = "ok"
    _FakeClient.error = None
    monkeypatch.setattr("src.api.services.voice_staging.httpx.AsyncClient", _FakeClient)
    yield


# --- disk staging -------------------------------------------------------------


def test_write_and_read_round_trip() -> None:
    written = voice_staging.write_staged("anchor_neutral_m", b"\x1f\x8b payload")

    assert written > 0
    voice_staging.clear_staged("anchor_neutral_m")


def test_read_staged_returns_the_original_wav() -> None:
    voice_staging.write_staged("anchor_neutral_m", voice_staging.gzip.compress(WAV))

    assert voice_staging.read_staged("anchor_neutral_m") == WAV


def test_clear_staged_tolerates_a_missing_file() -> None:
    voice_staging.clear_staged("anchor_neutral_m")

    assert not voice_staging.staged_path("anchor_neutral_m").exists()


def test_write_staged_rejects_an_invalid_name() -> None:
    for name in ("../etc/passwd", "Bad-Name", "a" * 65, "", "with space"):
        with pytest.raises(VoiceStagingError, match="invalid voice name") as excinfo:
            voice_staging.write_staged(name, b"payload")
        assert excinfo.value.retryable is False


def test_write_staged_rejects_an_empty_payload() -> None:
    with pytest.raises(VoiceStagingError, match="empty payload") as excinfo:
        voice_staging.write_staged("anchor_neutral_m", b"")
    assert excinfo.value.retryable is False


def test_write_staged_rejects_an_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_staging, "MAX_STAGED_BYTES", 8)

    with pytest.raises(VoiceStagingError, match="exceeds") as excinfo:
        voice_staging.write_staged("anchor_neutral_m", b"123456789")
    assert excinfo.value.retryable is False


def test_read_staged_reports_a_missing_file() -> None:
    with pytest.raises(VoiceStagingError, match="no staged audio") as excinfo:
        voice_staging.read_staged("anchor_neutral_m")
    assert excinfo.value.retryable is False


def test_read_staged_reports_a_corrupt_file() -> None:
    voice_staging.write_staged("anchor_neutral_m", b"not gzip at all")

    with pytest.raises(VoiceStagingError, match="is corrupt") as excinfo:
        voice_staging.read_staged("anchor_neutral_m")
    assert excinfo.value.retryable is False


# --- design-side HTTP client --------------------------------------------------


async def test_stage_from_design_ships_gzipped_audio() -> None:
    sent = await voice_staging.stage_from_design("anchor_neutral_m", WAV)

    assert sent > 0
    (call,) = _FakeClient.calls
    assert call["url"] == "http://server.test:8000/api/internal/voices/staged/anchor_neutral_m"
    assert call["headers"][voice_staging.STAGING_KEY_HEADER] == "staging-key"
    assert gzip.decompress(call["content"]) == WAV


async def test_stage_from_design_without_a_key_never_calls_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_staging_api_key", "")

    with pytest.raises(VoiceStagingError, match="not configured") as excinfo:
        await voice_staging.stage_from_design("anchor_neutral_m", WAV)

    assert excinfo.value.retryable is False
    assert _FakeClient.calls == []


async def test_stage_from_design_rejects_an_invalid_name() -> None:
    with pytest.raises(VoiceStagingError, match="invalid voice name") as excinfo:
        await voice_staging.stage_from_design("../etc/passwd", WAV)

    assert excinfo.value.retryable is False
    assert _FakeClient.calls == []


@pytest.mark.parametrize(
    ("status_code", "text", "retryable"),
    [
        (401, "invalid staging key", False),
        (503, "voice staging is not configured", True),
        (422, "invalid voice name", False),
        (408, "request timeout", True),
        (429, "slow down", True),
        (500, "internal error", True),
        (504, "gateway timeout", True),
    ],
)
async def test_stage_from_design_maps_server_answers(
    status_code: int, text: str, retryable: bool
) -> None:
    _FakeClient.status_code = status_code
    _FakeClient.text = text

    with pytest.raises(VoiceStagingError, match=f"{status_code}") as excinfo:
        await voice_staging.stage_from_design("anchor_neutral_m", WAV)

    assert excinfo.value.retryable is retryable


async def test_stage_from_design_keeps_network_errors_retryable() -> None:
    _FakeClient.error = httpx.ConnectError("connection reset")

    with pytest.raises(VoiceStagingError, match="failed") as excinfo:
        await voice_staging.stage_from_design("anchor_neutral_m", WAV)

    assert excinfo.value.retryable is True
    assert _FakeClient.calls == []
