"""Unit tests for the Phase 8.1 priority engine lease."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.api.db.database import get_db
from src.api.services.tts_lease import (
    PRIORITY_DESIGN,
    PRIORITY_DIGEST,
    PRIORITY_RENDER,
    RenderGate,
    TTSLeaseService,
    TTSLeaseTimeoutError,
    _iso,
)


def _service(**kwargs) -> TTSLeaseService:
    """Build a service tuned for fast tests (short TTL, fast polling)."""
    kwargs.setdefault("poll_seconds", 0.01)
    kwargs.setdefault("ttl_seconds", 60)
    kwargs.setdefault("waiter_ttl_seconds", 30)
    return TTSLeaseService(**kwargs)


async def _expire(scope: str = "engine") -> None:
    """Rewrite the lease's expiry into the past (simulates a dead holder)."""
    past = _iso(datetime.now(UTC) - timedelta(seconds=5))
    async with get_db(None) as db:
        await db.execute("UPDATE tts_leases SET expires_at = ? WHERE scope = ?", (past, scope))
        await db.commit()


async def _expire_waiter(owner: str) -> None:
    """Rewrite a waiter's expiry into the past (simulates a dead waiter)."""
    past = _iso(datetime.now(UTC) - timedelta(seconds=5))
    async with get_db(None) as db:
        await db.execute(
            "UPDATE tts_lease_waiters SET expires_at = ? WHERE owner = ?", (past, owner)
        )
        await db.commit()


# --- acquire / release -------------------------------------------------------


async def test_acquire_when_free_takes_the_lease() -> None:
    """The first caller claims a free lease and reads back as the holder."""
    service = _service()

    assert await service.acquire("run-1", PRIORITY_DIGEST) is True
    assert await service.holder() == "run-1"


async def test_acquire_while_held_reports_busy() -> None:
    """A second caller cannot take a live lease."""
    service = _service()
    await service.acquire("run-1", PRIORITY_DIGEST)

    assert await service.acquire("run-2", PRIORITY_DIGEST) is False
    assert await service.holder() == "run-1"


async def test_acquire_is_reentrant_for_the_holder() -> None:
    """A holder re-entering acquire renews instead of failing."""
    service = _service()
    await service.acquire("run-1", PRIORITY_DIGEST)

    assert await service.acquire("run-1", PRIORITY_DIGEST) is True
    assert await service.holder() == "run-1"


async def test_release_by_the_holder_frees_the_lease() -> None:
    """Releasing hands the engine to the next caller."""
    service = _service()
    await service.acquire("run-1", PRIORITY_DIGEST)
    await service.release("run-1")

    assert await service.holder() is None
    assert await service.acquire("run-2", PRIORITY_DIGEST) is True


async def test_release_by_a_non_holder_does_not_free() -> None:
    """A stray release must not free an engine somebody else is using."""
    service = _service()
    await service.acquire("run-1", PRIORITY_DIGEST)
    await service.release("run-2")

    assert await service.holder() == "run-1"


async def test_expired_lease_can_be_taken_over() -> None:
    """A holder that stopped renewing does not block the engine forever."""
    service = _service()
    await service.acquire("dead-run", PRIORITY_RENDER)
    await _expire()

    assert await service.holder() is None
    assert await service.acquire("run-2", PRIORITY_DIGEST) is True


async def test_renew_extends_a_live_lease() -> None:
    """renew() succeeds only for the current holder."""
    service = _service()
    await service.acquire("run-1", PRIORITY_DIGEST)
    await _expire()

    assert await service.renew("run-1") is True
    assert await service.holder() == "run-1"
    assert await service.renew("stranger") is False
    assert await service.holder() == "run-1"


# --- waiters -----------------------------------------------------------------


async def test_waiter_above_detects_a_higher_priority_waiter() -> None:
    """A queued digest is the signal that makes a render yield."""
    service = _service()
    await service.add_waiter("digest-run", PRIORITY_DIGEST)

    assert await service.waiter_above(PRIORITY_RENDER) is True
    assert await service.waiter_above(PRIORITY_DIGEST) is False


async def test_waiter_above_ignores_expired_rows() -> None:
    """A waiter that died before acquiring must not pause renders forever."""
    service = _service()
    await service.add_waiter("dead-digest", PRIORITY_DIGEST)
    await _expire_waiter("dead-digest")

    assert await service.waiter_above(PRIORITY_RENDER) is False


async def test_remove_waiter_clears_the_signal() -> None:
    """Removing the row takes the yield signal away."""
    service = _service()
    await service.add_waiter("digest-run", PRIORITY_DIGEST)
    await service.remove_waiter("digest-run")

    assert await service.waiter_above(PRIORITY_RENDER) is False


async def test_add_waiter_is_idempotent_per_owner() -> None:
    """Re-adding the same owner refreshes the row instead of duplicating it."""
    service = _service()
    await service.add_waiter("digest-run", PRIORITY_RENDER)
    await service.add_waiter("digest-run", PRIORITY_DIGEST)

    assert await service.waiter_above(PRIORITY_RENDER) is True


# --- queueing rules ----------------------------------------------------------


async def test_higher_priority_waiter_blocks_a_free_claim() -> None:
    """The digest gets there first even if a render polls while it is queued."""
    service = _service()
    await service.add_waiter("digest-run", PRIORITY_DIGEST)

    assert await service.acquire("render-run", PRIORITY_RENDER) is False
    assert await service.acquire("digest-run", PRIORITY_DIGEST) is True


async def test_fifo_orders_equal_priority_waiters() -> None:
    """The earliest waiter of a priority wins the free lease."""
    service = _service()
    await service.add_waiter("first", PRIORITY_DIGEST)
    await asyncio.sleep(0.01)
    await service.add_waiter("second", PRIORITY_DIGEST)

    assert await service.acquire("second", PRIORITY_DIGEST) is False
    assert await service.acquire("first", PRIORITY_DIGEST) is True


async def test_live_lease_is_never_stolen_by_a_higher_priority() -> None:
    """Hand-over goes through the yield protocol, not through pre-emption."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)
    await service.add_waiter("digest-run", PRIORITY_DIGEST)

    assert await service.acquire("digest-run", PRIORITY_DIGEST) is False
    assert await service.holder() == "render-run"


# --- wait_for_turn -----------------------------------------------------------


async def test_wait_for_turn_returns_immediately_when_free() -> None:
    """No queue, no sleeping."""
    service = _service()

    assert await service.wait_for_turn("run-1", PRIORITY_DIGEST) is True
    assert await service.holder() == "run-1"
    # The signal row is dropped as soon as the turn is granted.
    assert await service.waiter_above(PRIORITY_DIGEST) is False


async def test_wait_for_turn_waits_for_the_holder_to_finish() -> None:
    """A queued digest blocks, heartbeats, then acquires on release."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)
    ticks: list[int] = []

    async def _release_soon() -> None:
        await asyncio.sleep(0.05)
        await service.release("render-run")

    release = asyncio.create_task(_release_soon())
    granted = await service.wait_for_turn(
        "digest-run", PRIORITY_DIGEST, on_tick=lambda: ticks.append(1)
    )
    await release

    assert granted is True
    assert await service.holder() == "digest-run"
    assert ticks, "a waiter must heartbeat while it waits"


async def test_wait_for_turn_times_out_and_cleans_up() -> None:
    """A wedged lease gives up with False and leaves no waiter row behind."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)

    granted = await service.wait_for_turn("digest-run", PRIORITY_DIGEST, timeout_seconds=0.05)

    assert granted is False
    assert await service.waiter_above(PRIORITY_DIGEST) is False
    assert await service.holder() == "render-run"


async def test_wait_for_turn_timeout_raises_through_hold() -> None:
    """hold() turns a missed turn into an explicit, reportable error."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)

    with pytest.raises(TTSLeaseTimeoutError):
        async with service.hold("digest-run", PRIORITY_DIGEST, timeout_seconds=0.05):
            pytest.fail("the block must not run without the lease")


# --- RenderGate: the yield protocol a document render drives ------------------


async def test_gate_reports_whether_a_digest_is_waiting() -> None:
    """should_pause() is the predicate the render checks before each chunk."""
    service = _service()
    gate = RenderGate(service, "render-run", PRIORITY_RENDER)
    await service.acquire("render-run", PRIORITY_RENDER)

    assert await gate.should_pause() is False
    await service.add_waiter("digest-run", PRIORITY_DIGEST)
    assert await gate.should_pause() is True


async def test_gate_hands_the_engine_over_and_takes_it_back() -> None:
    """Release -> report PAUSED -> wait -> re-acquire -> report GENERATING."""
    service = _service()
    states: list[bool] = []
    gate = RenderGate(service, "render-run", PRIORITY_RENDER, on_state=states.append)
    await service.acquire("render-run", PRIORITY_RENDER)

    async def _digest_finishes() -> None:
        await asyncio.sleep(0.05)
        await service.acquire("digest-run", PRIORITY_DIGEST)
        await service.remove_waiter("digest-run")
        await service.release("digest-run")

    task = asyncio.create_task(_digest_finishes())
    await gate.yield_and_wait()
    await task

    assert states == [True, False]
    assert await service.holder() == "render-run"
    assert await service.holder() == "render-run", "the render keeps the engine after resuming"


async def test_gate_releases_before_waiting_so_the_digest_never_overlaps() -> None:
    """The digest must find a free engine, not one still being used."""
    service = _service()
    gate = RenderGate(service, "render-run", PRIORITY_RENDER)
    await service.acquire("render-run", PRIORITY_RENDER)
    await service.add_waiter("digest-run", PRIORITY_DIGEST)

    released = asyncio.Event()

    async def _observe() -> None:
        while not released.is_set():
            if await service.holder() is None:
                released.set()
                return
            await asyncio.sleep(0.01)

    observer = asyncio.create_task(_observe())
    waiter = asyncio.create_task(gate.yield_and_wait())
    await asyncio.wait_for(released.wait(), timeout=2)
    # The engine is free while the render is still waiting: no overlap window.
    assert await service.holder() is None
    await service.remove_waiter("digest-run")
    await waiter
    await observer


async def test_gate_is_a_noop_when_the_lease_is_disabled() -> None:
    """TTS_LEASE_ENABLED=false means no yield, no state change, no waiting."""
    service = _service(enabled=False)
    states: list[bool] = []
    gate = RenderGate(service, "render-run", on_state=states.append)

    assert await gate.should_pause() is False
    await gate.yield_and_wait()

    assert states == []


# --- hold --------------------------------------------------------------------


async def test_hold_releases_when_the_block_finishes() -> None:
    """Normal exit hands the lease back."""
    service = _service()

    async with service.hold("run-1", PRIORITY_DIGEST):
        assert await service.holder() == "run-1"

    assert await service.holder() is None


async def test_hold_releases_when_the_block_raises() -> None:
    """A failed synthesis must not leave the engine locked."""
    service = _service()

    with pytest.raises(RuntimeError, match="boom"):
        async with service.hold("run-1", PRIORITY_DIGEST):
            raise RuntimeError("boom")

    assert await service.holder() is None
    assert await service.acquire("run-2", PRIORITY_DIGEST) is True


async def test_hold_renews_in_the_background() -> None:
    """A long block keeps the lease alive past the TTL."""
    service = _service(ttl_seconds=1)

    async with service.hold("run-1", PRIORITY_DIGEST):
        # Pretend the holder stopped renewing long ago: only the background
        # renewal task can bring the lease back to life.
        past = _iso(datetime.now(UTC) - timedelta(seconds=30))
        async with get_db(None) as db:
            await db.execute(
                "UPDATE tts_leases SET acquired_at = ?, expires_at = ? WHERE owner = ?",
                (past, past, "run-1"),
            )
            await db.commit()
        assert await service.holder() is None
        await asyncio.sleep(0.6)

        assert await service.holder() == "run-1"


# --- kill switch -------------------------------------------------------------


async def test_disabled_lease_is_a_noop() -> None:
    """TTS_LEASE_ENABLED=false restores the previous, unserialized behaviour."""
    service = _service(enabled=False)

    assert await service.acquire("run-1", PRIORITY_DIGEST) is True
    assert await service.acquire("run-2", PRIORITY_DIGEST) is True
    assert await service.holder() is None
    assert await service.waiter_above(PRIORITY_RENDER) is False
    await service.add_waiter("run-1", PRIORITY_DIGEST)
    await service.remove_waiter("run-1")
    await service.release("run-1")
    assert await service.renew("run-1") is False
    assert await service.wait_for_turn("run-3", PRIORITY_DIGEST) is True

    async with service.hold("run-4", PRIORITY_DIGEST):
        pass


# --- P8: the voice design window joins the lease ------------------------------


async def test_design_priority_outranks_digest_and_render() -> None:
    """The window must queue ahead of the delivery deadline, let alone renders."""
    assert PRIORITY_DESIGN > PRIORITY_DIGEST > PRIORITY_RENDER


async def test_design_holder_blocks_digest_and_render() -> None:
    """A live design window keeps the engine for itself."""
    service = _service()
    await service.acquire("design-window", PRIORITY_DESIGN, note="design-window")

    assert await service.acquire("digest-run", PRIORITY_DIGEST) is False
    assert await service.acquire("render-run", PRIORITY_RENDER) is False
    assert await service.holder() == "design-window"


async def test_design_waiter_blocks_a_free_claim() -> None:
    """Even a free engine is withheld while the design window queues."""
    service = _service()
    await service.add_waiter("design-window", PRIORITY_DESIGN)

    assert await service.acquire("digest-run", PRIORITY_DIGEST) is False
    assert await service.acquire("render-run", PRIORITY_RENDER) is False
    assert await service.holder() is None


async def test_render_yields_to_a_design_waiter() -> None:
    """A render in flight pauses on its own once the window joins the queue."""
    service = _service()
    gate = RenderGate(service, "render-run", PRIORITY_RENDER)
    await service.acquire("render-run", PRIORITY_RENDER)
    await service.add_waiter("design-window", PRIORITY_DESIGN)

    assert await service.waiter_above(PRIORITY_RENDER) is True
    assert await gate.should_pause() is True


async def test_wait_for_turn_design_takes_over_after_render_releases() -> None:
    """The window waits out a live render and then holds, with no overlap."""
    service = _service()
    await service.acquire("render-run", PRIORITY_RENDER)
    observed: list[str | None] = []

    async def _watch() -> None:
        while await service.holder() != "design-window":
            observed.append(await service.holder())
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(_watch())

    async def _render_finishes() -> None:
        await asyncio.sleep(0.05)
        await service.release("render-run")

    closer = asyncio.create_task(_render_finishes())
    granted = await service.wait_for_turn("design-window", PRIORITY_DESIGN, note="design-window")
    await watcher
    await closer

    assert granted is True
    assert await service.holder() == "design-window"
    # A brief free gap during the handoff is the point; any other *owner*
    # would mean the two held the engine at the same time.
    assert set(observed) <= {"render-run", None}


async def test_note_round_trips_through_hold_and_holder_info() -> None:
    """holder_info() reports the stated reason while the window holds."""
    service = _service()

    async with service.hold("design-window", PRIORITY_DESIGN, note="design-window"):
        info = await service.holder_info()
        assert info is not None
        assert info["owner"] == "design-window"
        assert info["note"] == "design-window"
        assert info["priority"] == PRIORITY_DESIGN


async def test_renew_preserves_the_note() -> None:
    """A renew must not wipe why the engine is held."""
    service = _service()
    await service.acquire("design-window", PRIORITY_DESIGN, note="design-window")

    assert await service.renew("design-window") is True
    info = await service.holder_info()
    assert info is not None and info["note"] == "design-window"


async def test_takeover_of_an_expired_lease_records_the_note() -> None:
    """Stealing an abandoned digest lease rewrites the reason too."""
    service = _service()
    await service.acquire("digest-run", PRIORITY_DIGEST)
    await _expire()

    assert await service.acquire("design-window", PRIORITY_DESIGN, note="design-window") is True
    info = await service.holder_info()
    assert info is not None and info["note"] == "design-window"


async def test_holder_info_is_none_when_free_or_expired() -> None:
    """No holder (or a dead one) is reported as no holder at all."""
    service = _service()
    assert await service.holder_info() is None

    await service.acquire("render-run", PRIORITY_RENDER)
    await _expire()
    assert await service.holder_info() is None


async def test_waiters_lists_live_waiters_in_priority_order() -> None:
    """status only ever shows waiters that are still alive."""
    service = _service()
    await service.add_waiter("render-run", PRIORITY_RENDER)
    await service.add_waiter("design-window", PRIORITY_DESIGN)

    waiters = await service.waiters()

    assert [w["owner"] for w in waiters] == ["design-window", "render-run"]
    assert waiters[0]["priority"] == PRIORITY_DESIGN

    await _expire_waiter("render-run")
    assert [w["owner"] for w in await service.waiters()] == ["design-window"]


async def test_gate_operator_pause_hands_the_engine_back_and_waits() -> None:
    """A paused audiobook releases the engine and re-acquires it on resume."""
    service = _service()
    paused = {"on": True}
    states: list[bool] = []
    gate = RenderGate(
        service,
        "render-run",
        PRIORITY_RENDER,
        on_state=states.append,
        user_paused=lambda: paused["on"],
    )
    await service.acquire("render-run", PRIORITY_RENDER)

    assert await gate.should_pause() is True
    waiter = asyncio.create_task(gate.yield_and_wait())
    for _ in range(200):
        if await service.holder() is None:
            break
        await asyncio.sleep(0.01)
    assert await service.holder() is None, "the engine is free while the operator holds"

    paused["on"] = False
    await waiter

    assert states == [True, False]
    assert await service.holder() == "render-run"


async def test_gate_operator_pause_works_with_the_lease_disabled() -> None:
    """TTS_LEASE_ENABLED=false must not disable the operator's pause button."""
    service = _service(enabled=False)
    paused = {"on": True}
    states: list[bool] = []

    async def _paused() -> bool:
        return paused["on"]

    gate = RenderGate(service, "render-run", on_state=states.append, user_paused=_paused)

    assert await gate.should_pause() is True
    waiter = asyncio.create_task(gate.yield_and_wait())
    await asyncio.sleep(0.05)
    assert not waiter.done(), "a disabled lease still honours the operator pause"

    paused["on"] = False
    await waiter
    assert states == [True, False]
