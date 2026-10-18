"""Operator CLI for the voice design window's GPU lease (merge phase P8).

The design window is a manual, host-level operation: it stops the gateway and
Ollama to hand the GPU over to the voice design worker. Instead of coordinating
by hand (and racing in-flight synthesis), the window now claims the same
priority lease the digest and the renders use, at the highest priority.

    The lease is claimed from *this* CLI on the host that owns ``data/news.db``
    (Shape A), never from the design worker itself, which keeps the invariant that
    neither voice worker role touches the database.

    The whole claim-and-hold lives inside one ``try/finally``: a cancellation can
    land while ``wait_for_turn`` is still cleaning up its waiter row *after* the
    claim went through, and the release must run in that case too or the engine
    would stay held until the lease TTL expires.

    Examples:
        python -m src.workers.design_lease open --wait-timeout 900
        python -m src.workers.design_lease status
        python -m src.workers.design_lease close
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

import structlog

from src.api.services.tts_lease import PRIORITY_DESIGN, TTSLeaseService
from src.workers.run_worker import configure_logging

logger = structlog.get_logger(__name__)

#: Owner id of the design window. Stable on purpose: ``status`` and ``close``
#: need a predictable name to report and to release on behalf of.
OWNER = "design-window"
#: Note recorded on the lease so an operator can tell a design window apart
#: from a digest run just by reading ``holder_info()``.
NOTE = "design-window"


def _service() -> TTSLeaseService:
    """Build the lease service from settings (shared SQLite, engine scope).

    Returns:
        A service bound to the settings-driven database and scope.
    """
    return TTSLeaseService()


async def open_window(lease: TTSLeaseService, wait_timeout: float | None) -> int:
    """Claim the engine, renew until cancelled, and release on the way out.

    Waits for the current holder to yield (the wait itself makes live renders
    pause), then renews in a loop until the task is cancelled by a signal. The
    whole claim lives inside one ``try`` so the release runs even when the
    cancellation lands while ``wait_for_turn`` is still dropping its waiter row
    *after* the claim went through — otherwise the engine would stay held until
    the lease TTL expires. If the process is killed outright, the TTL covers it.

    Args:
        lease: The lease service to use.
        wait_timeout: Max seconds to wait for the turn; None waits until the
            caller's own budget ends it.

    Returns:
        Process exit code: 0 on a clean release, 1 when the lease was lost
        while holding, 2 when the turn did not come within ``wait_timeout``.
    """
    try:
        acquired = await lease.wait_for_turn(
            OWNER, PRIORITY_DESIGN, timeout_seconds=wait_timeout, note=NOTE
        )
        if not acquired:
            logger.error(
                "design_lease_timeout",
                wait_timeout=wait_timeout,
                hint="a digest or render is still on the engine",
            )
            return 2

        logger.info("design_lease_acquired", owner=OWNER)
        interval = max(lease.ttl_seconds / 3.0, 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await lease.renew(OWNER):
                logger.error("design_lease_lost", owner=OWNER)
                return 1
    finally:
        # Belt and braces: a cancellation may have interrupted wait_for_turn's
        # own cleanup, leaving a waiter row behind that would stall later
        # digest claims for the waiter TTL.
        await lease.remove_waiter(OWNER)
        await lease.release(OWNER)
        logger.info("design_lease_released", owner=OWNER)


async def status_payload(lease: TTSLeaseService) -> dict[str, Any]:
    """Collect the current lease state as a JSON-serialisable dict.

    Args:
        lease: The lease service to inspect.

    Returns:
        Dict with ``enabled``, ``scope``, ``holder`` (or None) and the list of
        live ``waiters``.
    """
    return {
        "enabled": lease.enabled,
        "scope": lease.scope,
        "holder": await lease.holder_info(),
        "waiters": await lease.waiters(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the CLI arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        The parsed namespace with ``command`` and, for ``open``,
        ``wait_timeout``.
    """
    parser = argparse.ArgumentParser(prog="design_lease", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    open_parser = sub.add_parser("open", help="Claim the engine for a design window")
    open_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=900.0,
        help="Max seconds to wait for the current holder to yield (default 900).",
    )
    sub.add_parser("status", help="Print the lease state as JSON")
    sub.add_parser("close", help="Release the lease held by the design window")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Dispatch a parsed command.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    lease = _service()

    if args.command == "status":
        payload = await status_payload(lease)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "close":
        await lease.release(OWNER)
        logger.info("design_lease_released", owner=OWNER)
        return 0

    task = asyncio.current_task()
    if task is not None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
    try:
        return await open_window(lease, args.wait_timeout)
    except asyncio.CancelledError:
        logger.info("design_lease_interrupted", owner=OWNER)
        return 0


async def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``python -m src.workers.design_lease``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    configure_logging()
    return await _run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
