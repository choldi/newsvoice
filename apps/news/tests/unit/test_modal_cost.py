"""Unit tests for Modal wall-clock cost measurement (Phase 7.17)."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.api.db.database import ModalSpendRepository, get_db
from src.core import modal_cost
from src.core.config import settings
from src.core.modal_cost import (
    CallCost,
    RateCard,
    episode_tags,
    get_cost_context,
    measure,
    record_call,
    set_cost_context,
)

_CARD = RateCard(
    cpu_per_hour=Decimal("0.0473"),
    mem_per_hour=Decimal("0.008"),
    gpu_per_hour={"l4": Decimal("0.80"), "h100": Decimal("3.95")},
)


async def _last_row() -> dict[str, object]:
    """Return the most recent modal_spend row as a dict.

    Returns:
        Column name to value for the newest row.
    """
    async with get_db() as db:
        db.row_factory = None
        async with db.execute(
            "SELECT cost_source, gpu_type, wall_seconds, billed_delta_usd, cost_usd "
            "FROM modal_spend ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    return {
        "cost_source": row[0],
        "gpu_type": row[1],
        "wall_seconds": row[2],
        "billed_delta_usd": row[3],
        "cost_usd": row[4],
    }


# --- cost math -------------------------------------------------------------


def test_compute_cost_gpu_only() -> None:
    cost = modal_cost.compute_cost_usd(
        wall_seconds=3600, gpu="l4", card=_CARD, cpu_cores=0.0, memory_gib=0.0
    )
    assert cost == pytest.approx(0.80)


def test_compute_cost_includes_cpu_and_memory() -> None:
    cost = modal_cost.compute_cost_usd(
        wall_seconds=3600, gpu=None, card=_CARD, cpu_cores=2.0, memory_gib=4.0
    )
    assert cost == pytest.approx(2 * 0.0473 + 4 * 0.008)


def test_compute_cost_scales_linearly_with_time() -> None:
    one = modal_cost.compute_cost_usd(wall_seconds=60, gpu="h100", card=_CARD)
    two = modal_cost.compute_cost_usd(wall_seconds=120, gpu="h100", card=_CARD)
    assert two == pytest.approx(2 * one)


def test_rate_card_unknown_gpu_is_free() -> None:
    assert _CARD.gpu_rate(None) == Decimal(0)
    assert _CARD.gpu_rate("mystery") == Decimal(0)


def test_fallback_rates_match_settings() -> None:
    card = modal_cost._fallback_rates()
    assert card.gpu_per_hour["l4"] == Decimal(str(settings.modal_usd_per_gpu_second)) * 3600
    assert card.gpu_per_hour["h100"] == Decimal(str(settings.modal_usd_per_gpu_second_h100)) * 3600


async def test_rate_card_is_static_fallback() -> None:
    """The live workspace rate card is retired; pricing is always static."""
    card = await modal_cost.rate_card()
    assert card == modal_cost._fallback_rates()


# --- ledger writes ---------------------------------------------------------


def test_cost_context_round_trips() -> None:
    set_cost_context(collection="tech", run_id="r1", stage="tts")
    assert get_cost_context() == {"collection": "tech", "run_id": "r1", "stage": "tts"}


async def test_record_call_writes_measured_columns() -> None:
    set_cost_context(collection="tech", run_id="r1", stage="tts")
    cost = CallCost(
        backend="qwen3_tts",
        gpu_type="l4",
        wall_seconds=12.5,
        gpu_seconds=12.5,
        cost_usd=0.0028,
    )
    await record_call(cost, audio_seconds=9.0)
    row = await _last_row()
    assert row["cost_source"] == "runtime_rates"
    assert row["gpu_type"] == "l4"
    assert row["wall_seconds"] == pytest.approx(12.5)
    assert row["billed_delta_usd"] == pytest.approx(0.0)


async def test_record_call_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ModalSpendRepository, "record", AsyncMock(side_effect=RuntimeError("db down"))
    )
    cost = CallCost(
        backend="qwen3_tts", gpu_type="l4", wall_seconds=1.0, gpu_seconds=1.0, cost_usd=0.0
    )
    await record_call(cost)  # must not raise


# --- measure ---------------------------------------------------------------


async def test_measure_returns_result_and_cost() -> None:
    async def _call() -> bytes:
        return b"audio"

    result, cost = await measure(_call(), backend="qwen3_tts")
    assert result == b"audio"
    assert cost is not None
    assert cost.gpu_type == "l4"
    assert cost.cost_usd >= 0.0
    assert cost.billed_delta_usd == 0.0
    row = await _last_row()
    assert row["cost_source"] == "runtime_rates"
    assert row["billed_delta_usd"] == pytest.approx(0.0)


async def test_measure_disabled_tracking_returns_none_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_cost_tracking_enabled", False)

    async def _call() -> int:
        return 7

    result, cost = await measure(_call(), backend="judge")
    assert result == 7
    assert cost is None


# --- tags ------------------------------------------------------------------


async def test_episode_tags_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "modal_enabled", False)
    async with episode_tags("tech", "r1"):
        pass


async def test_episode_tags_best_effort_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "modal_enabled", True)
    monkeypatch.setattr(settings, "modal_tags_enabled", True)
    apply = AsyncMock(return_value=None)
    monkeypatch.setattr(modal_cost, "_apply_tags", apply)
    async with episode_tags("tech", "r1"):
        pass
    apply.assert_awaited_once()
    assert apply.await_args.args[0]["collection"] == "tech"


def test_tags_are_disabled_by_default() -> None:
    """``_App`` poisons the Modal client, so tagging stays opt-in."""
    assert settings.modal_tags_enabled is False


# --- retired billing -------------------------------------------------------


def test_source_has_no_private_billing_probe() -> None:
    """The private workspace/billing SDK is gone: it poisoned the client."""
    source = Path(modal_cost.__file__).read_text(encoding="utf-8")
    assert "modal._workspace" not in source
    assert "_load_rates" not in source
    assert "billing_snapshot" not in source
    assert "record_billing_delta" not in source


# --- schema ----------------------------------------------------------------


async def test_modal_spend_has_cost_columns() -> None:
    async with get_db() as db:
        async with db.execute("PRAGMA table_info(modal_spend)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
    assert {"wall_seconds", "cost_source", "gpu_type", "billed_delta_usd"} <= columns
