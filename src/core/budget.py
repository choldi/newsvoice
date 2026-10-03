"""Modal spend budget guards (Phase 7.8).

Four layers protect the Modal budget:

1. Preflight: cache probe, cooldown and estimate are checked before any
   ``.spawn()``, so a free cache hit is never blocked by an exhausted budget.
2. Runtime: the Modal function caps audio length and chunks.
3. Leak mitigation: the activity always terminates the spawned container.
4. Accounting: every call is recorded in ``modal_spend`` and alerted on.

This module holds the pure decision logic; the activity wires it up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.api.db.database import ModalSpendRepository
from src.core.config import settings


@dataclass(frozen=True)
class BudgetDecision:
    """Outcome of a budget preflight check.

    Attributes:
        allowed: Whether the Modal call may proceed.
        reason: Human-readable explanation when blocked.
        estimate_usd: Estimated cost of the pending call.
    """

    allowed: bool
    reason: str = ""
    estimate_usd: float = 0.0


def day_start() -> datetime:
    """Return the start of the current UTC day.

    Returns:
        Timestamp at 00:00 UTC today.
    """
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start() -> datetime:
    """Return the start of the current UTC month.

    Returns:
        Timestamp at 00:00 UTC on the first of the month.
    """
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def daily_spend() -> float:
    """Return today's Modal spend in UTC.

    Returns:
        Total cost in USD.
    """
    return await ModalSpendRepository().spent_since(day_start().isoformat())


async def monthly_spend() -> float:
    """Return this month's Modal spend in UTC.

    Returns:
        Total cost in USD.
    """
    return await ModalSpendRepository().spent_since(month_start().isoformat())


def estimate_gpu_seconds(char_count: int) -> float:
    """Estimate the GPU seconds a render will cost.

    Args:
        char_count: Characters to synthesize.

    Returns:
        Estimated GPU seconds.
    """
    audio_seconds = char_count / settings.modal_estimated_chars_per_second
    return audio_seconds * settings.modal_estimated_rtf


def estimate_cost(char_count: int) -> float:
    """Estimate the USD cost of a render.

    Args:
        char_count: Characters to synthesize.

    Returns:
        Estimated cost in USD.
    """
    return estimate_gpu_seconds(char_count) * settings.modal_usd_per_gpu_second


def estimate_judge_cost(prompt_chars: int, completion_chars: int) -> float:
    """Estimate the USD cost of one Modal LLM judge call (Phase 7.13).

    Reuses the TTS chars-to-GPU-seconds ratio as a throughput proxy: a judge
    decodes far fewer characters per second than a TTS model emits audio, so this
    estimate is deliberately generous and the real ledger row carries the measured
    wall-clock time next to it.

    Args:
        prompt_chars: Characters sent to the judge.
        completion_chars: Characters expected back.

    Returns:
        Estimated cost in USD.
    """
    return estimate_cost(prompt_chars + completion_chars)


async def budget_headroom() -> float:
    """Return the smallest remaining headroom across both windows.

    Returns:
        USD still available before a cap is hit.
    """
    daily = settings.modal_budget_usd_daily - await daily_spend()
    monthly = settings.modal_budget_usd_monthly - await monthly_spend()
    return min(daily, monthly)


async def cooldown_remaining() -> float:
    """Return the seconds left in the cooldown window.

    Returns:
        Seconds remaining, or 0 when the cooldown has elapsed.
    """
    last = await ModalSpendRepository().last_call_ts()
    if last is None:
        return 0.0
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
    remaining = settings.modal_cooldown_minutes * 60 - elapsed
    return max(0.0, remaining)


async def preflight(*, char_count: int, cache_hit: bool) -> BudgetDecision:
    """Decide whether a Modal render may proceed.

    A cache hit is always allowed: it costs nothing, so an exhausted budget
    must never block audio that is already on disk.

    Args:
        char_count: Characters to synthesize.
        cache_hit: True when the render is already cached.

    Returns:
        The decision, with a reason when blocked.
    """
    if cache_hit:
        return BudgetDecision(allowed=True, reason="cache_hit")
    estimate = estimate_cost(char_count)
    remaining = await cooldown_remaining()
    if remaining > 0:
        return BudgetDecision(
            allowed=False,
            reason=f"cooldown active for {remaining:.0f}s",
            estimate_usd=estimate,
        )
    headroom = await budget_headroom()
    if estimate > headroom:
        return BudgetDecision(
            allowed=False,
            reason=f"estimate ${estimate:.4f} exceeds headroom ${headroom:.4f}",
            estimate_usd=estimate,
        )
    return BudgetDecision(allowed=True, estimate_usd=estimate)
