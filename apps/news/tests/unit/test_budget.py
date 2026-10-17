"""Unit tests for the Modal budget guard and spend ledger (Phase 7.8)."""

from datetime import UTC, datetime

import pytest

from src.api.db.database import ModalSpendRepository
from src.core.budget import (
    BudgetDecision,
    budget_headroom,
    cooldown_remaining,
    day_start,
    estimate_cost,
    estimate_gpu_seconds,
    month_start,
    preflight,
)
from src.core.config import settings

# --- estimate --------------------------------------------------------------


def test_estimate_gpu_seconds_uses_throughput_model() -> None:
    assert estimate_gpu_seconds(1750) == pytest.approx(100 * settings.modal_estimated_rtf)


def test_estimate_cost_multiplies_by_gpu_rate() -> None:
    assert estimate_cost(1750) == pytest.approx(
        100 * settings.modal_estimated_rtf * settings.modal_usd_per_gpu_second
    )


def test_estimate_cost_scales_with_length() -> None:
    assert estimate_cost(3500) == pytest.approx(2 * estimate_cost(1750))


# --- window boundaries -----------------------------------------------------


def test_day_start_is_midnight_utc() -> None:
    start = day_start()
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.tzinfo is UTC


def test_month_start_is_first_of_month() -> None:
    start = month_start()
    assert (start.day, start.hour, start.minute) == (1, 0, 0)
    assert start.tzinfo is UTC


# --- preflight -------------------------------------------------------------


async def test_preflight_allows_cache_hit() -> None:
    decision = await preflight(char_count=100_000, cache_hit=True)
    assert decision.allowed is True
    assert decision.reason == "cache_hit"


async def test_preflight_allows_when_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_budget_usd_daily", 100.0)
    monkeypatch.setattr(settings, "modal_budget_usd_monthly", 1000.0)
    decision = await preflight(char_count=1000, cache_hit=False)
    assert decision.allowed is True
    assert decision.estimate_usd > 0


async def test_preflight_blocks_when_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_budget_usd_daily", 0.0001)
    monkeypatch.setattr(settings, "modal_budget_usd_monthly", 0.0001)
    decision = await preflight(char_count=10_000, cache_hit=False)
    assert decision.allowed is False
    assert "headroom" in decision.reason


async def test_preflight_blocks_during_cooldown() -> None:
    repo = ModalSpendRepository()
    await repo.record(
        collection="tech",
        stage="tts",
        backend="qwen3_tts",
        gpu_seconds=1.0,
        cost_usd=0.001,
    )
    remaining = await cooldown_remaining()
    assert remaining > 0
    decision = await preflight(char_count=1000, cache_hit=False)
    assert decision.allowed is False
    assert "cooldown" in decision.reason


async def test_cooldown_remaining_zero_without_calls() -> None:
    assert await cooldown_remaining() == 0.0


async def test_budget_headroom_is_smallest_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_budget_usd_daily", 5.0)
    monkeypatch.setattr(settings, "modal_budget_usd_monthly", 100.0)
    assert await budget_headroom() == pytest.approx(5.0)


# --- ModalSpendRepository --------------------------------------------------


async def test_record_and_spent_since() -> None:
    repo = ModalSpendRepository()
    await repo.record(
        collection="tech", stage="tts", backend="qwen3_tts", gpu_seconds=10.0, cost_usd=0.01
    )
    since = day_start().isoformat()
    assert await repo.spent_since(since) == pytest.approx(0.01)


async def test_spent_since_excludes_old_records() -> None:
    repo = ModalSpendRepository()
    await repo.record(
        collection="tech", stage="tts", backend="qwen3_tts", gpu_seconds=10.0, cost_usd=0.01
    )
    future = datetime(2999, 1, 1, tzinfo=UTC).isoformat()
    assert await repo.spent_since(future) == 0.0


async def test_last_call_ts_none_when_empty() -> None:
    assert await ModalSpendRepository().last_call_ts() is None


async def test_last_call_ts_set_after_record() -> None:
    repo = ModalSpendRepository()
    await repo.record(
        collection="tech", stage="tts", backend="qwen3_tts", gpu_seconds=1.0, cost_usd=0.001
    )
    last = await repo.last_call_ts()
    assert last is not None
    datetime.fromisoformat(last)


async def test_record_marks_cache_hit() -> None:
    repo = ModalSpendRepository()
    await repo.record(
        collection="tech",
        stage="tts",
        backend="qwen3_tts",
        gpu_seconds=0.0,
        cost_usd=0.0,
        cache_hit=True,
    )
    rows = await repo.spent_since("2000-01-01T00:00:00+00:00")
    assert rows == 0.0


def test_budget_decision_defaults() -> None:
    decision = BudgetDecision(allowed=True)
    assert decision.reason == ""
    assert decision.estimate_usd == 0.0
