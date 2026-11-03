"""Priority engine lease serializing TTS synthesis across processes (Phase 8.1).

The digest TTS step and the long-document render are served by the same engines
(the ``tts-api`` gateway on the host, Microsoft's edge-tts endpoint) but run in
**different containers**, so an ``asyncio.Semaphore`` inside either worker
coordinates nothing. Both already mount the same SQLite file, so the lease lives
there (migration ``011_tts_lease.sql``): no new dependency, no Redis.

Protocol
--------
A high-priority waiter (the daily digest) writes its row into
``tts_lease_waiters`` *before* it asks for the lease. A low-priority holder
(the document render) checks that table between chunks; when it sees a waiter
above its own priority it lets go, waits for that waiter to disappear, and
re-acquires. Because the waiter row lands **before** the acquire, the holder
releases before the digest starts, so the two never synthesize concurrently.

The voice design window (merge phase P8) is the third participant: it claims
the same lease at :data:`PRIORITY_DESIGN` with ``note="design-window"``, which
both blocks later digest claims and makes live renders yield *before* the
window script stops the gateway.

Acquisition rules (all in single statements, so SQLite's one-writer model makes
them atomic across processes):

1. No lease row yet and no higher/equal-priority waiter ahead of me -> claim.
2. I already own it -> renew (the holder is alive and re-entering).
3. The lease has expired (holder crashed without releasing, TTL elapsed) and no
   higher-priority waiter exists and no equal-priority waiter asked earlier
   (FIFO) -> claim.
4. Otherwise -> busy.

A live lease is **never** stolen from its holder. Pre-empting it would overlap
with chunks already in flight; the yield protocol above is what hands the engine
over, and the TTL is only the safety net for a dead holder.

This module is imported from activities and services only — never from a
workflow, which must not do I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.api.db.database import get_db
from src.core.config import get_settings
from src.core.tracing import trace_function

logger = structlog.get_logger(__name__)

#: Priority of the long renders (document, lesson, narration): they hold the
#: engine for hours and are the ones that must yield.
PRIORITY_RENDER = 0
#: Priority of the daily digest TTS: few thousand characters, but it is the one
#: with a delivery deadline.
PRIORITY_DIGEST = 10
#: Priority of the voice design window (merge phase P8): it holds the engine
#: for the whole window, outranks the digest, and is what makes renders yield
#: *before* the window script stops the gateway.
PRIORITY_DESIGN = 20

#: Callback invoked once per poll while waiting for a turn (activity heartbeat).
TickCallback = Callable[[], None]
#: Predicate answering whether the operator paused this render (sync or async).
PauseCallback = Callable[[], Any]


class TTSLeaseError(RuntimeError):
    """Base error for the engine lease."""


class TTSLeaseTimeoutError(TTSLeaseError):
    """Raised when a waiter could not get its turn within the budget."""


def _iso(value: datetime) -> str:
    """Format a datetime as a fixed-width ISO-8601 UTC string.

    Microseconds are always rendered so the strings sort lexicographically in
    the same order as they do chronologically.

    Args:
        value: Datetime to format.

    Returns:
        ISO-8601 string with a fixed number of digits.
    """
    return value.isoformat(timespec="microseconds")


def _parse(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp stored in the lease tables.

    Args:
        value: Raw timestamp from the database.

    Returns:
        The parsed datetime, or None when the value is malformed (a corrupt row
        must not wedge synthesis forever).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class TTSLeaseService:
    """Cross-process, priority-aware lease over one contended TTS engine."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        scope: str | None = None,
        ttl_seconds: int | None = None,
        poll_seconds: float | None = None,
        waiter_ttl_seconds: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Initialize the service from settings with optional overrides.

        Args:
            db_path: SQLite path. Defaults to ``settings.database_path``.
            scope: Lease row name. Defaults to ``settings.tts_lease_scope``.
            ttl_seconds: Holder lease duration. Defaults to settings.
            poll_seconds: Waiter poll interval. Defaults to settings.
            waiter_ttl_seconds: Waiter row lifetime. Defaults to settings.
            enabled: Kill switch. Defaults to ``settings.tts_lease_enabled``.
        """
        settings = get_settings()
        self._db_path = db_path
        self._scope = scope or settings.tts_lease_scope
        self._ttl = float(
            ttl_seconds if ttl_seconds is not None else settings.tts_lease_ttl_seconds
        )
        self._poll = float(
            poll_seconds if poll_seconds is not None else settings.tts_lease_poll_seconds
        )
        self._waiter_ttl = float(
            waiter_ttl_seconds
            if waiter_ttl_seconds is not None
            else settings.tts_lease_waiter_ttl_seconds
        )
        self._enabled = settings.tts_lease_enabled if enabled is None else bool(enabled)

    # --- introspection ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Return whether the lease is active (False = every call is a no-op)."""
        return self._enabled

    @property
    def scope(self) -> str:
        """Return the lease scope this instance manages."""
        return self._scope

    @property
    def poll_seconds(self) -> float:
        """Return how often a waiter re-checks for its turn."""
        return self._poll

    @property
    def ttl_seconds(self) -> float:
        """Return how long a holder keeps the lease without renewing."""
        return self._ttl

    async def holder(self) -> str | None:
        """Read the current holder of the lease.

        Returns:
            The owner id, or None when the lease is free, expired (abandoned)
            or the lease is disabled.
        """
        info = await self.holder_info()
        return None if info is None else str(info["owner"])

    async def holder_info(self) -> dict[str, Any] | None:
        """Read who holds the lease and why (merge phase P8).

        Returns:
            Dict with ``owner``, ``note``, ``priority``, ``acquired_at`` and
            ``expires_at`` (all strings), or None when the lease is free,
            expired (abandoned) or disabled. ``note`` is the holder's stated
            reason — ``"design-window"`` for the voice design window, ``""``
            for the digest and renders, which are identified by owner alone.
        """
        if not self._enabled:
            return None
        async with get_db(self._db_path) as db:
            async with db.execute(
                "SELECT owner, note, priority, acquired_at, expires_at "
                "FROM tts_leases WHERE scope = ?",
                (self._scope,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        expires = _parse(str(row["expires_at"]))
        if expires is None or expires <= datetime.now(UTC):
            return None
        return {
            "owner": str(row["owner"]),
            "note": str(row["note"]),
            "priority": int(row["priority"]),
            "acquired_at": str(row["acquired_at"]),
            "expires_at": str(row["expires_at"]),
        }

    # --- waiters ------------------------------------------------------------

    async def add_waiter(self, owner: str, priority: int) -> None:
        """Register this owner as waiting for a turn.

        The row doubles as the **yield signal**: a holder polling
        :meth:`waiter_above` sees it before the waiter has the lease, which is
        what makes the hand-over non-overlapping.

        Args:
            owner: Stable id of the waiter (the workflow id).
            priority: Higher wins; renders sit at :data:`PRIORITY_RENDER`, the
                digest at :data:`PRIORITY_DIGEST`.
        """
        if not self._enabled:
            return
        now = datetime.now(UTC)
        query = """
            INSERT INTO tts_lease_waiters (owner, priority, requested_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner) DO UPDATE SET
                priority = excluded.priority,
                expires_at = excluded.expires_at
        """
        async with get_db(self._db_path) as db:
            await db.execute(
                query,
                (owner, priority, _iso(now), _iso(now + timedelta(seconds=self._waiter_ttl))),
            )
            await db.commit()

    async def remove_waiter(self, owner: str) -> None:
        """Drop a waiter row.

        Must run on every exit path: a row left behind by a crashed digest
        would pause document renders until the waiter TTL elapsed.

        Args:
            owner: Id passed to :meth:`add_waiter`.
        """
        if not self._enabled:
            return
        async with get_db(self._db_path) as db:
            await db.execute("DELETE FROM tts_lease_waiters WHERE owner = ?", (owner,))
            await db.commit()

    async def waiters(self) -> list[dict[str, Any]]:
        """List the live waiters queued for the engine.

        Returns:
            One dict per live waiter (``owner``, ``priority``,
            ``requested_at``, ``expires_at``) ordered by priority then request
            time. Empty when the lease is disabled.
        """
        if not self._enabled:
            return []
        now = _iso(datetime.now(UTC))
        async with get_db(self._db_path) as db:
            async with db.execute(
                "SELECT owner, priority, requested_at, expires_at "
                "FROM tts_lease_waiters WHERE expires_at > ? "
                "ORDER BY priority DESC, requested_at ASC",
                (now,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "owner": str(row["owner"]),
                "priority": int(row["priority"]),
                "requested_at": str(row["requested_at"]),
                "expires_at": str(row["expires_at"]),
            }
            for row in rows
        ]

    async def waiter_above(self, priority: int) -> bool:
        """Report whether somebody outranking ``priority`` is waiting.

        This is the yield predicate: a render holding the lease at
        :data:`PRIORITY_RENDER` must let go as soon as a digest is queued.

        Args:
            priority: Priority of the caller.

        Returns:
            True when a live waiter with a strictly higher priority exists.
        """
        if not self._enabled:
            return False
        now = _iso(datetime.now(UTC))
        async with get_db(self._db_path) as db:
            async with db.execute(
                "SELECT 1 FROM tts_lease_waiters WHERE priority > ? AND expires_at > ? LIMIT 1",
                (priority, now),
            ) as cursor:
                row = await cursor.fetchone()
        return row is not None

    # --- lease --------------------------------------------------------------

    @trace_function(component="api.services.tts_lease")
    async def acquire(self, owner: str, priority: int, *, note: str = "") -> bool:
        """Try to take the lease without blocking.

        Args:
            owner: Stable id of the holder (the workflow id).
            priority: Priority of the request.
            note: Stated reason for holding the engine (``"design-window"``);
                persisted on claim and takeover, kept as-is on renew.

        Returns:
            True when this owner now holds the lease.
        """
        if not self._enabled:
            return True
        now = datetime.now(UTC)
        now_iso = _iso(now)
        expires = _iso(now + timedelta(seconds=self._ttl))

        async with get_db(self._db_path) as db:
            # 1. Nobody holds it, and nobody outranks us in the queue.
            await db.execute(
                """
                INSERT INTO tts_leases (scope, owner, priority, note, acquired_at, expires_at)
                SELECT ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM tts_leases WHERE scope = ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM tts_lease_waiters
                      WHERE expires_at > ? AND priority > ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM tts_lease_waiters
                      WHERE expires_at > ? AND priority = ?
                        AND requested_at < (
                            SELECT requested_at FROM tts_lease_waiters WHERE owner = ?
                        )
                  )
                ON CONFLICT(scope) DO NOTHING
                """,
                (
                    self._scope,
                    owner,
                    priority,
                    note,
                    now_iso,
                    expires,
                    self._scope,
                    now_iso,
                    priority,
                    now_iso,
                    priority,
                    owner,
                ),
            )
            # 2. We already own it: renew in place (we are alive).
            cursor = await db.execute(
                """
                UPDATE tts_leases
                SET acquired_at = ?, expires_at = ?, priority = ?
                WHERE scope = ? AND owner = ?
                """,
                (now_iso, expires, priority, self._scope, owner),
            )
            if cursor.rowcount:
                await db.commit()
                return True
            # 3. Abandoned by a holder that stopped renewing: take over, still
            #    respecting the queue of live waiters.
            cursor = await db.execute(
                """
                UPDATE tts_leases
                SET owner = ?, priority = ?, note = ?, acquired_at = ?, expires_at = ?
                WHERE scope = ? AND expires_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM tts_lease_waiters
                      WHERE expires_at > ? AND priority > ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM tts_lease_waiters
                      WHERE expires_at > ? AND priority = ?
                        AND requested_at < (
                            SELECT requested_at FROM tts_lease_waiters WHERE owner = ?
                        )
                  )
                """,
                (
                    owner,
                    priority,
                    note,
                    now_iso,
                    expires,
                    self._scope,
                    now_iso,
                    now_iso,
                    priority,
                    now_iso,
                    priority,
                    owner,
                ),
            )
            acquired = bool(cursor.rowcount)
            await db.commit()
        if not acquired:
            logger.debug("tts_lease_busy", scope=self._scope, owner=owner, priority=priority)
        return acquired

    async def release(self, owner: str) -> None:
        """Release the lease when this owner holds it.

        A release from a non-holder is silently ignored so a double release
        (or one from a holder that was already replaced) cannot free an engine
        somebody else is legitimately using.

        Args:
            owner: Id that was passed to :meth:`acquire`.
        """
        if not self._enabled:
            return
        async with get_db(self._db_path) as db:
            await db.execute(
                "DELETE FROM tts_leases WHERE scope = ? AND owner = ?",
                (self._scope, owner),
            )
            await db.commit()

    async def renew(self, owner: str) -> bool:
        """Extend the lease of a live holder.

        Long synthesis calls (a 5000-character digest is one uninterrupted
        edge-tts stream that can run for minutes) must not look abandoned, so
        holders run a renewal loop for the duration of the hold.

        Args:
            owner: Id that acquired the lease.

        Returns:
            True when the lease was still ours and got extended.
        """
        if not self._enabled:
            return False
        now = datetime.now(UTC)
        async with get_db(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE tts_leases SET expires_at = ? WHERE scope = ? AND owner = ?",
                (
                    _iso(now + timedelta(seconds=self._ttl)),
                    self._scope,
                    owner,
                ),
            )
            await db.commit()
        return bool(cursor.rowcount)

    # --- waiting ------------------------------------------------------------

    async def wait_for_turn(
        self,
        owner: str,
        priority: int,
        *,
        on_tick: TickCallback | None = None,
        timeout_seconds: float | None = None,
        note: str = "",
    ) -> bool:
        """Register as a waiter and block until the lease can be taken.

        By default the wait is bounded only by the caller's own
        ``start_to_close_timeout``, which is the honest budget: whoever holds
        the engine may legitimately hold it for as long as its synthesis takes
        (a gateway render of a full digest is minutes, a book is hours), so any
        shorter ceiling here would fail a run that was simply queuing behind a
        live one. ``timeout_seconds`` exists for callers that want a stricter
        deadline of their own.

        Args:
            owner: Stable id of the waiter.
            priority: Priority of the request.
            on_tick: Called once per poll while waiting (activity heartbeat).
            timeout_seconds: Give up after this long; None waits until the
                activity's own timeout ends it.
            note: Stated reason persisted on the claim (see :meth:`acquire`).

        Returns:
            True when the lease is now held by ``owner``.

        Raises:
            asyncio.CancelledError: Propagates when the caller is cancelled.
        """
        if not self._enabled:
            return True
        deadline = None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        await self.add_waiter(owner, priority)
        try:
            while True:
                if await self.acquire(owner, priority, note=note):
                    return True
                if on_tick is not None:
                    on_tick()
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(self._poll)
        finally:
            # We either hold the lease (the row is no longer needed as a signal)
            # or we gave up (the row must not outlive us). Either way: drop it.
            await self.remove_waiter(owner)

    @asynccontextmanager
    async def hold(
        self,
        owner: str,
        priority: int,
        *,
        on_tick: TickCallback | None = None,
        timeout_seconds: float | None = None,
        note: str = "",
    ) -> AsyncIterator[None]:
        """Hold the lease for the duration of the block, renewing in the background.

        Args:
            owner: Stable id of the holder (the workflow id).
            priority: Priority of the request.
            on_tick: Called once per poll while waiting for the turn.
            timeout_seconds: Max seconds to wait for the turn.
            note: Stated reason persisted on the claim (see :meth:`acquire`).

        Yields:
            Nothing; the lease is held for the duration of the block.

        Raises:
            TTSLeaseTimeoutError: When no turn came within ``timeout_seconds``.
                The caller turns it into a non-retryable activity error so the
                run records a clear reason instead of hanging.
        """
        if not self._enabled:
            yield
            return

        acquired = await self.wait_for_turn(
            owner, priority, on_tick=on_tick, timeout_seconds=timeout_seconds, note=note
        )
        if not acquired:
            raise TTSLeaseTimeoutError(
                f"no TTS engine turn for {owner!r} within {float(timeout_seconds):.0f}s"
            )

        stop = asyncio.Event()
        # A third of the TTL leaves two failed attempts before the lease is
        # considered abandoned; the floor only keeps a pathological TTL from
        # spinning the loop.
        renew_interval = max(self._ttl / 3.0, 0.05)
        renew_task = asyncio.create_task(self._renew_loop(owner, stop, renew_interval))
        try:
            yield
        finally:
            stop.set()
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            await self.release(owner)
            logger.info("tts_lease_released", scope=self._scope, owner=owner)

    async def _renew_loop(self, owner: str, stop: asyncio.Event, interval: float) -> None:
        """Keep a held lease alive until the hold block ends.

        While a render is yielding, it has released the lease on purpose, so an
        absent lease is expected and must not be reported as a problem. A lease
        that exists but belongs to somebody else is a genuine loss of ownership
        and is warned about.

        Args:
            owner: Holder id to renew.
            stop: Set by the owner when the block finishes.
            interval: Seconds between renewals (a third of the TTL).
        """
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                if await self.renew(owner):
                    continue
                if await self.holder() is not None:
                    logger.warning("tts_lease_renew_lost", scope=self._scope, owner=owner)
            except Exception as exc:  # noqa: BLE001 - a transient DB error must not kill the hold
                logger.warning("tts_lease_renew_failed", owner=owner, error=str(exc))


class RenderGate:
    """The slice of the lease protocol a long render drives from inside its loop.

    A document render holds the engine for hours, so it cannot simply block
    until its turn (that is the digest's job): it has to *hand the engine over*
    the moment somebody outranking it shows up, wait out the whole synthesis,
    and take it back. That sequence is the yield protocol, and it lives here so
    the service only has to ask two questions — "should I pause?" and "pause
    then" — and to report the two states the GUI displays.

    The gate assumes the caller already holds the lease (via
    :meth:`TTSLeaseService.hold`); it releases and re-acquires the *same* owner,
    so the surrounding hold block still releases correctly at the end.
    """

    def __init__(
        self,
        lease: TTSLeaseService,
        owner: str,
        priority: int = PRIORITY_RENDER,
        *,
        on_tick: TickCallback | None = None,
        on_state: Callable[[bool], Any] | None = None,
        user_paused: PauseCallback | None = None,
    ) -> None:
        """Initialize the gate.

        Args:
            lease: Lease service the gate drives.
            owner: Id that holds the lease (the workflow id).
            priority: Priority of the holder; a waiter *above* it is what makes
                the render yield.
            on_tick: Called once per poll while waiting (activity heartbeat).
            on_state: Called with True when the render pauses and False when it
                resumes (``audio_jobs.status`` follows it). It may be sync or
                async; an awaitable result is awaited.
            user_paused: Optional predicate answering whether the operator
                paused this run (``audio_jobs.paused``). It may be sync or
                async; an awaitable result is awaited. Unlike the lease yield,
                an operator pause must work even when the lease is disabled.
        """
        self._lease = lease
        self._owner = owner
        self._priority = priority
        self._on_tick = on_tick
        self._on_state = on_state
        self._user_paused = user_paused
        self._paused = False
        self._back: asyncio.Event | None = None

    async def _operator_paused(self) -> bool:
        """Ask the optional operator-pause predicate.

        Returns:
            True when the operator asked this render to pause.
        """
        if self._user_paused is None:
            return False
        outcome = self._user_paused()
        if inspect.isawaitable(outcome):
            return bool(await outcome)
        return bool(outcome)

    async def should_pause(self) -> bool:
        """Report whether the render must stop before the next chunk.

        Returns:
            True when the engine must be handed over (a higher-priority waiter
            is queued) or the operator paused this run.
        """
        if await self._operator_paused():
            return True
        return await self._lease.waiter_above(self._priority)

    async def _notify(self, paused: bool) -> None:
        """Report a pause/resume transition to the caller.

        Args:
            paused: True when the engine was handed over, False when it came
                back.

        Raises:
            Exception: Whatever the callback raises; a failed status update must
                surface instead of leaving the GUI on the wrong state.
        """
        if self._on_state is None:
            return
        outcome = self._on_state(paused)
        if inspect.isawaitable(outcome):
            await outcome

    async def yield_and_wait(self) -> None:
        """Release the engine, wait out the priority waiter, then take it back.

        The lease is released *before* the wait starts, which is what makes the
        hand-over non-overlapping: the digest acquires an engine nobody is
        synthesizing with, and the render resumes only after the digest has
        released it again. The loop is bounded only by the activity's own
        ``start_to_close_timeout``, because the digest may legitimately hold the
        engine for as long as its synthesis takes.

        Chunks run concurrently, so only the first one to notice the queue
        performs the hand-over; the others wait on it and resume together. That
        keeps ``on_state`` a pair of transitions (PAUSED, then GENERATING)
        instead of one event per chunk, which is what the GUI shows.

        Raises:
            asyncio.CancelledError: Propagates when the caller is cancelled.
        """
        if self._paused:
            # Another chunk is already handing the engine over: wait it out so
            # no chunk starts synthesizing while the digest owns the engine.
            if self._back is not None:
                await self._back.wait()
            return
        operator_paused = await self._operator_paused()
        if not self._lease.enabled and not operator_paused:
            return
        self._back = asyncio.Event()
        self._paused = True
        logger.info(
            "tts_engine_yielding",
            owner=self._owner,
            priority=self._priority,
            operator=operator_paused,
        )
        await self._lease.release(self._owner)
        await self._notify(True)
        try:
            # The operator pause is independent of the lease: it must hold the
            # render even when the engine is not contended (or the lease is
            # disabled), and a digest waiter keeps it paused after a resume.
            while await self._operator_paused() or await self._lease.waiter_above(self._priority):
                if self._on_tick is not None:
                    self._on_tick()
                await asyncio.sleep(self._lease.poll_seconds)
            while not await self._lease.acquire(self._owner, self._priority):
                if self._on_tick is not None:
                    self._on_tick()
                await asyncio.sleep(self._lease.poll_seconds)
            await self._notify(False)
            logger.info("tts_engine_yielded", owner=self._owner, priority=self._priority)
        finally:
            # Always unblock the sibling chunks, even if the wait failed: they
            # re-check should_pause() and decide for themselves from there.
            self._paused = False
            waiting, self._back = self._back, None
            if waiting is not None:
                waiting.set()
