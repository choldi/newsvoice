"""Unit tests for the audiobook Temporal activities (heartbeat + bookkeeping)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import AudioJobsRepository
from src.api.services.audiobook import AUDIOBOOK_COLLECTION, AUDIOBOOK_KIND
from src.workers import audiobook_activities

RUN_ID = "audiobook-act1"


class _NoLease:
    """Lease stand-in: the activity tests never contend for the engine."""

    @asynccontextmanager
    async def hold(self, *args: Any, **kwargs: Any):
        yield


class _FakeService:
    """AudiobookService stand-in with a configurable render outcome."""

    def __init__(self, *, delay: float = 0.0, error: BaseException | None = None) -> None:
        self.delay = delay
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def render_m4b(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            m4b_path="/data/audiobooks/audio/libro.m4b",
            chapters=2,
            duration_seconds=12.0,
        )


async def _start_job(run_id: str = RUN_ID) -> None:
    """Create the STARTING row the API writes at submit time."""
    await AudioJobsRepository().start_job(
        run_id, collection=AUDIOBOOK_COLLECTION, kind=AUDIOBOOK_KIND
    )


async def _job(run_id: str = RUN_ID) -> dict:
    """Fetch the audio job row for a run."""
    job = await AudioJobsRepository().get_job(run_id, kind=AUDIOBOOK_KIND)
    assert job is not None, f"job {run_id} not found"
    return job


def _payload(**overrides: Any) -> dict[str, Any]:
    """Minimal render payload with the voice options the activity forwards."""
    payload = {
        "run_id": RUN_ID,
        "text": "Hola mundo.",
        "title": "Libro",
        "voice": "es_ma_juan__qwen3_tts",
        "language": "es",
        "engine": "qwen3_tts",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _no_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the cross-process lease: these tests only exercise the activity."""
    monkeypatch.setattr(audiobook_activities, "TTSLeaseService", lambda: _NoLease())


async def test_render_heartbeats_while_synthesizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long synthesis must not silence the heartbeat until the timeout."""
    monkeypatch.setattr(audiobook_activities, "AUDIOBOOK_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    service = _FakeService(delay=0.05)
    monkeypatch.setattr(audiobook_activities, "AudiobookService", lambda: service)
    await _start_job()

    beats: list[tuple[Any, ...]] = []
    environment = ActivityEnvironment()
    environment.on_heartbeat = lambda *args: beats.append(args)

    result = await environment.run(audiobook_activities.render_audiobook_activity, _payload())

    assert len(beats) >= 1, "the heartbeat loop must run during render_m4b"
    assert result["audio_path"] == "/data/audiobooks/audio/libro.m4b"
    assert (await _job())["status"] == "FINISHED_OK"


async def test_render_cancellation_closes_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heartbeat timeout cancels with CancelledError; the row must not stick."""
    service = _FakeService(error=asyncio.CancelledError())
    monkeypatch.setattr(audiobook_activities, "AudiobookService", lambda: service)
    await _start_job()

    with pytest.raises(asyncio.CancelledError):
        await ActivityEnvironment().run(audiobook_activities.render_audiobook_activity, _payload())

    job = await _job()
    assert job["status"] == "FINISHED_ERROR"
    assert "cancelled" in (job["error"] or "")


async def test_render_failure_records_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary exception is still recorded before becoming non-retryable."""
    service = _FakeService(error=RuntimeError("gateway down"))
    monkeypatch.setattr(audiobook_activities, "AudiobookService", lambda: service)
    await _start_job()

    with pytest.raises(Exception, match="gateway down"):
        await ActivityEnvironment().run(audiobook_activities.render_audiobook_activity, _payload())

    job = await _job()
    assert job["status"] == "FINISHED_ERROR"
    assert "gateway down" in (job["error"] or "")


async def test_fail_activity_closes_a_generating_job() -> None:
    """The workflow safety net records a run no activity could report."""
    await _start_job()

    await ActivityEnvironment().run(
        audiobook_activities.fail_audiobook_activity,
        {"run_id": RUN_ID, "error": "audiobook render failed: worker crashed"},
    )

    job = await _job()
    assert job["status"] == "FINISHED_ERROR"
    assert "worker crashed" in (job["error"] or "")


async def test_fail_activity_never_overwrites_a_terminal_job() -> None:
    """A run that already finished keeps its outcome and its error column."""
    await _start_job()
    await AudioJobsRepository().finish_job(
        RUN_ID,
        status="FINISHED_OK",
        audio_path="/data/audiobooks/audio/libro.m4b",
        kind=AUDIOBOOK_KIND,
    )

    await ActivityEnvironment().run(
        audiobook_activities.fail_audiobook_activity,
        {"run_id": RUN_ID, "error": "late failure"},
    )

    job = await _job()
    assert job["status"] == "FINISHED_OK"
    assert job["error"] is None
    assert job["audio_path"] == "/data/audiobooks/audio/libro.m4b"


def test_audiobook_worker_registers_the_failure_activity() -> None:
    """The safety-net activity must be registered on the queue that runs it."""
    from src.workers.run_audiobook_worker import worker_activities

    registered = {getattr(activity, "__name__") for activity in worker_activities()}
    assert "fail_audiobook_activity" in registered
