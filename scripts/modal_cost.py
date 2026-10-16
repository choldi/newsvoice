"""Inspect Modal billing, rates and per-call cost (Phase 7.17).

A thin CLI over the same APIs the app records with, for ad-hoc measurement:
read the workspace billing cycle, break cost down by interval and tag, print
the live rate card, or diff the billing summary across an arbitrary command.

Usage (from the repository root, so .env and Modal credentials are found):

    .venv/bin/python scripts/modal_cost.py summary
    .venv/bin/python scripts/modal_cost.py rates
    .venv/bin/python scripts/modal_cost.py report --for today -r h --tags '*'
    .venv/bin/python scripts/modal_cost.py diff -- pytest -q

Modal exposes no credit balance, only cost: ``summary`` reports the credits
*consumed* this cycle (the ``Credits`` adjustment), not what is left.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modal._workspace import _Workspace  # noqa: E402


def _parse_date(value: str) -> datetime:
    """Parse an ISO date (``YYYY-MM-DD``) or ``today``/``yesterday``.

    Args:
        value: Date string.

    Returns:
        Midnight UTC for that date.
    """
    if value == "today":
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "yesterday":
        day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=1)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _summary() -> dict[str, Any]:
    """Return the workspace billing summary as a JSON-friendly dict.

    Returns:
        Dict with metered/billed cost, credits consumed and the breakdowns.
    """
    summary = await _Workspace.from_context().billing.summary()
    return {
        "window_start": summary.start.isoformat(),
        "window_end": summary.end.isoformat(),
        "metered_cost": str(summary.metered_cost),
        "billed_cost": str(summary.billed_cost),
        "credits_consumed": str(abs(summary.adjustments.get("Credits", 0))),
        "adjustments": {k: str(v) for k, v in summary.adjustments.items()},
        "metered_cost_breakdown": {k: str(v) for k, v in summary.metered_cost_breakdown.items()},
    }


async def _rates() -> dict[str, str]:
    """Return the live workspace rate card as a dict of strings.

    Returns:
        Mapping of rate name to value.
    """
    rates = await _Workspace.from_context().billing.rates()
    return {key: str(rates[key]) for key in rates}


async def _report(
    *, start: datetime, end: datetime | None, resolution: str, tags: list[str] | None
) -> list[dict[str, Any]]:
    """Return the billing report rows as JSON-friendly dicts.

    Args:
        start: Inclusive report start.
        end: Exclusive report end.
        resolution: ``d`` or ``h``.
        tags: Tag names to include, or ``None``.

    Returns:
        One dict per reported interval.
    """
    items = await _Workspace.from_context().billing.report(
        start=start, end=end, resolution=resolution, tag_names=tags
    )
    return [
        {
            "object_id": item.object_id,
            "description": item.description,
            "environment": item.environment_name,
            "interval_start": item.interval_start.isoformat(),
            "cost": str(item.cost),
            "cost_by_resource": {k: str(v) for k, v in item.cost_by_resource.items()},
            "tags": dict(item.tags),
        }
        for item in items
    ]


async def _diff(command: list[str]) -> dict[str, Any]:
    """Run a command and report the billing-summary change it caused.

    Args:
        command: Command and arguments to run.

    Returns:
        Dict with before/after metered cost, the delta, and the exit code.
    """
    before = await _Workspace.from_context().billing.summary()
    proc = subprocess.run(command, check=False)
    after = await _Workspace.from_context().billing.summary()
    return {
        "command": command,
        "exit_code": proc.returncode,
        "metered_before": str(before.metered_cost),
        "metered_after": str(after.metered_cost),
        "metered_delta": str(after.metered_cost - before.metered_cost),
        "credits_before": str(abs(before.adjustments.get("Credits", 0))),
        "credits_after": str(abs(after.adjustments.get("Credits", 0))),
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="Show the workspace billing summary.")
    sub.add_parser("rates", help="Show the live workspace rate card.")

    report = sub.add_parser("report", help="Show cost per interval, optionally by tag.")
    report.add_argument("--start", default="today", help="Start date (ISO, 'today', 'yesterday').")
    report.add_argument("--end", default=None, help="Exclusive end date (ISO).")
    report.add_argument("-r", "--resolution", default="h", choices=["d", "h"], help="Interval.")
    report.add_argument(
        "-t", "--tags", default=None, help="Comma-separated tag names; '*' for all."
    )

    diff = sub.add_parser("diff", help="Diff the billing summary across a command.")
    diff.add_argument("run", nargs=argparse.REMAINDER, help="Command to run, after '--'.")

    return parser


async def _main_async(args: argparse.Namespace) -> dict[str, Any] | list[Any]:
    """Dispatch the parsed arguments to the matching coroutine.

    Args:
        args: Parsed CLI arguments.

    Returns:
        JSON-friendly result for the chosen subcommand.
    """
    if args.command == "summary":
        return await _summary()
    if args.command == "rates":
        return await _rates()
    if args.command == "report":
        start = _parse_date(args.start)
        end = _parse_date(args.end) if args.end else None
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
        return await _report(start=start, end=end, resolution=args.resolution, tags=tags)
    run = list(args.run)
    if run and run[0] == "--":
        run = run[1:]
    if not run:
        raise SystemExit("diff needs a command after '--'")
    return await _diff(run)


def main() -> None:
    """Entry point: parse arguments and print the JSON result."""
    args = _build_parser().parse_args()
    result = asyncio.run(_main_async(args))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
