"""Unit tests for the design-window GPU lease CLI (merge phase P8)."""

import asyncio
import json

import pytest

from src.api.services.tts_lease import PRIORITY_RENDER, TTSLeaseService
from src.workers.design_lease import OWNER, _run, open_window, parse_args, status_payload


def _service(**kwargs) -> TTSLeaseService:
    """Build a service tuned for fast tests (short TTL, fast polling)."""
    kwargs.setdefault("poll_seconds", 0.01)
    kwargs.setdefault("ttl_seconds", 60)
    kwargs.setdefault("waiter_ttl_seconds", 30)
    return TTSLeaseService(**kwargs)


# --- parse_args ---------------------------------------------------------------


def test_parse_args_defaults() -> None:
    """`open` waits 900s by default; status/close are plain subcommands."""
    args = parse_args(["open"])
    assert args.command == "open"
    assert args.wait_timeout == 900.0

    args = parse_args(["open", "--wait-timeout", "5"])
    assert args.wait_timeout == 5.0

    assert parse_args(["status"]).command == "status"
    assert parse_args(["close"]).command == "close"


def test_parse_args_requires_a_command() -> None:
    """A bare invocation is a usage error, not a silent default."""
    with pytest.raises(SystemExit):
        parse_args([])


# --- status -------------------------------------------------------------------


async def test_status_payload_reports_a_free_engine() -> None:
    """JSON with enabled/holder/waiters, holder null when nothing holds it."""
    payload = await status_payload(_service())

    assert payload["enabled"] is True
    assert payload["scope"] == "engine"
    assert payload["holder"] is None
    assert payload["waiters"] == []


async def test_status_command_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    """The script the status section parses gets valid JSON on stdout."""
    rc = await _run(parse_args(["status"]))

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["enabled"] is True
    assert data["holder"] is None


# --- open ---------------------------------------------------------------------


async def test_open_acquires_holds_and_releases_on_cancel() -> None:
    """The CLI takes the lease, says why, and frees it when interrupted."""
    service = _service()
    task = asyncio.create_task(_run(parse_args(["open", "--wait-timeout", "5"])))

    for _ in range(300):
        if await service.holder() == OWNER:
            break
        await asyncio.sleep(0.01)
    assert await service.holder() == OWNER
    info = await service.holder_info()
    assert info is not None and info["note"] == "design-window"

    task.cancel()
    assert await task == 0

    assert await service.holder() is None
    assert await service.waiters() == []


async def test_open_releases_when_cancelled_while_still_waiting() -> None:
    """Interrupting during the wait leaves no waiter row and takes nothing."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)
    task = asyncio.create_task(_run(parse_args(["open", "--wait-timeout", "30"])))
    await asyncio.sleep(0.05)

    task.cancel()
    assert await task == 0

    assert await service.holder() == "render-run"
    assert await service.waiters() == []


async def test_open_releases_when_cancelled_during_the_waiter_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel inside wait_for_turn's post-claim cleanup must not leak the lease.

    wait_for_turn drops its waiter row in a finally *after* the claim is already
    visible; a SIGINT landing there used to skip the CLI's release entirely and
    leave the engine held until the lease TTL expired.
    """
    service = _service()
    claim_cleanup_started = asyncio.Event()
    calls = 0
    original_remove = TTSLeaseService.remove_waiter

    async def slow_remove(self: TTSLeaseService, owner: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            claim_cleanup_started.set()
            await asyncio.sleep(30)
        await original_remove(self, owner)

    monkeypatch.setattr(TTSLeaseService, "remove_waiter", slow_remove)
    task = asyncio.create_task(_run(parse_args(["open", "--wait-timeout", "5"])))
    await asyncio.wait_for(claim_cleanup_started.wait(), timeout=5)
    assert await service.holder() == OWNER

    task.cancel()
    assert await task == 0

    assert await service.holder() is None
    assert await service.waiters() == []


async def test_open_times_out_without_touching_a_live_holder() -> None:
    """No turn within the budget exits 2 and leaves the holder untouched."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)

    rc = await open_window(service, 0.05)

    assert rc == 2
    assert await service.holder() == "render-run"
    assert await service.waiters() == []


async def test_open_releases_when_the_renewal_loses_the_lease() -> None:
    """Losing the lease mid-window exits 1 instead of pretending to hold it."""
    service = _service(ttl_seconds=0.3)

    # Steal only once OWNER demonstrably holds: a clock-based steal races
    # with wait_for_turn's first acquire under a loaded loop (pytest-xdist),
    # leaving the renewal loop renewing a lease nobody will ever take away.
    async def _steal() -> None:
        while await service.holder() != OWNER:
            await asyncio.sleep(0.01)
        await service.release(OWNER)
        await service.acquire("digest-run", 10)

    stealer = asyncio.create_task(_steal())
    rc = await open_window(service, None)
    await stealer

    assert rc == 1


# --- close --------------------------------------------------------------------


async def test_close_releases_and_is_idempotent() -> None:
    """`close` hands the engine back and is safe to run twice."""
    service = _service()
    await service.acquire(OWNER, 10, note="design-window")

    assert await _run(parse_args(["close"])) == 0
    assert await service.holder() is None

    assert await _run(parse_args(["close"])) == 0
    assert await service.holder() is None
