"""Measure the real cost of Modal calls from wall-clock time (Phase 7.17).

Modal exposes no per-call price and its private workspace/billing API poisons
the process-global client (every later RPC hangs), so billing reconciliation is
retired: the wall-clock cost -- call seconds multiplied by the static rate card
-- is the ledger. ``billed_delta_usd`` stays in the schema as a retired column
that is always zero.

Everything here is best-effort: a failure degrades to a log line and never
propagates into the render path. ``modal`` is imported lazily so importing this
module (for instance from the local worker) needs no Modal runtime.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar

import structlog

from src.core.config import settings

logger = structlog.get_logger(__name__)

T = TypeVar("T")

#: Modal GPU attached to each backend; ``None`` means the call is CPU-only.
BACKEND_GPU: dict[str, str | None] = {
    "qwen3_tts": "l4",
    "zonos2": "h100",
    "voxcpm2": "l4",
    "judge": "l4",
    "design": "l4",
    "upload_voice": None,
}


@dataclass(frozen=True)
class RateCard:
    """The Modal rate card, in USD per hour, as reported by the workspace.

    Attributes:
        cpu_per_hour: Cost of one CPU core for an hour.
        mem_per_hour: Cost of one GiB of memory for an hour.
        gpu_per_hour: Mapping of short GPU name (``l4``, ``h100``) to hourly cost.
    """

    cpu_per_hour: Decimal
    mem_per_hour: Decimal
    gpu_per_hour: dict[str, Decimal] = field(default_factory=dict)

    def gpu_rate(self, gpu: str | None) -> Decimal:
        """Return the hourly cost of a GPU, or zero when it is CPU-only.

        Args:
            gpu: Short GPU name, or ``None``.

        Returns:
            Hourly cost in USD.
        """
        if gpu is None:
            return Decimal(0)
        return self.gpu_per_hour.get(gpu, Decimal(0))


@dataclass(frozen=True)
class CallCost:
    """The measured cost of one Modal call.

    Attributes:
        backend: Backend that served the call.
        gpu_type: Short GPU name, or ``None`` for CPU-only calls.
        wall_seconds: Client-observed wall-clock seconds around the call.
        gpu_seconds: GPU seconds billed (equal to ``wall_seconds`` for GPU runs).
        cost_usd: Cost derived from wall-clock time and the rate card.
        billed_delta_usd: Retired billing-summary delta; always zero.
        cost_source: ``runtime_rates``.
    """

    backend: str
    gpu_type: str | None
    wall_seconds: float
    gpu_seconds: float
    cost_usd: float
    billed_delta_usd: float = 0.0
    cost_source: str = "runtime_rates"


#: Per-call context (collection, run id, stage) recorded alongside each row.
_cost_context: ContextVar[dict[str, str]] = ContextVar("modal_cost_context", default={})

_APP: Any = None


def set_cost_context(*, collection: str = "", run_id: str = "", stage: str = "") -> None:
    """Set the collection/run/stage recorded with every subsequent Modal row.

    Args:
        collection: Collection that triggered the calls.
        run_id: Digest or narration run id.
        stage: Pipeline stage (``tts``, ``llm_verify``, ...).
    """
    _cost_context.set({"collection": collection, "run_id": run_id, "stage": stage})


def get_cost_context() -> dict[str, str]:
    """Return the current per-call cost context.

    Returns:
        Dict with ``collection``, ``run_id`` and ``stage`` keys.
    """
    return _cost_context.get()


def compute_cost_usd(
    *,
    wall_seconds: float,
    gpu: str | None,
    card: RateCard,
    cpu_cores: float | None = None,
    memory_gib: float | None = None,
) -> float:
    """Compute the USD cost of a call from its wall-clock time.

    GPU dominates the bill; CPU and memory are included for completeness and are
    read from settings unless explicitly overridden.

    Args:
        wall_seconds: Client-observed wall-clock seconds.
        gpu: Short GPU name, or ``None`` for CPU-only calls.
        card: Rate card to price the call with.
        cpu_cores: CPU cores billed; defaults to ``settings.modal_cpu_cores_per_call``.
        memory_gib: Memory GiB billed; defaults to ``settings.modal_memory_gib_per_call``.

    Returns:
        Cost in USD.
    """
    cores = settings.modal_cpu_cores_per_call if cpu_cores is None else cpu_cores
    memory = settings.modal_memory_gib_per_call if memory_gib is None else memory_gib
    per_hour = card.cpu_per_hour * Decimal(str(cores))
    per_hour += card.mem_per_hour * Decimal(str(memory))
    per_hour += card.gpu_rate(gpu)
    return float(per_hour * Decimal(str(wall_seconds)) / Decimal(3600))


def _fallback_rates() -> RateCard:
    """Build a static rate card from settings, used when the live one fails.

    Returns:
        A rate card with the configured L4/H100 GPU costs and Modal's published
        CPU/memory rates.
    """
    return RateCard(
        cpu_per_hour=Decimal("0.04730"),
        mem_per_hour=Decimal("0.00800"),
        gpu_per_hour={
            "l4": Decimal(str(settings.modal_usd_per_gpu_second)) * Decimal(3600),
            "h100": Decimal(str(settings.modal_usd_per_gpu_second_h100)) * Decimal(3600),
        },
    )


async def rate_card() -> RateCard:
    """Return the static rate card built from settings.

    The live Modal rate card came from the private workspace API, which poisons
    the client, so pricing always uses the configured L4/H100 rates.

    Returns:
        The rate card to price calls with.
    """
    return _fallback_rates()


def reset_caches() -> None:
    """Drop the cached Modal app handle (mainly for tests)."""
    global _APP
    _APP = None


async def record_call(cost: CallCost, *, audio_seconds: float | None = None) -> None:
    """Persist one measured call to the Modal spend ledger.

    Best-effort: a database failure is logged and swallowed so accounting never
    breaks a render.

    Args:
        cost: The measured call cost.
        audio_seconds: Audio produced, when known.
    """
    from src.api.db.database import ModalSpendRepository

    context = get_cost_context()
    try:
        await ModalSpendRepository().record(
            collection=context.get("collection") or "unknown",
            stage=context.get("stage") or "unknown",
            backend=cost.backend,
            run_id=context.get("run_id") or None,
            gpu_seconds=round(cost.gpu_seconds, 2),
            cost_usd=round(cost.cost_usd, 6),
            audio_seconds=audio_seconds,
            wall_seconds=round(cost.wall_seconds, 2),
            cost_source=cost.cost_source,
            gpu_type=cost.gpu_type,
            billed_delta_usd=round(cost.billed_delta_usd, 6),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort accounting
        logger.warning("modal_spend_record_failed", error=str(exc))
    logger.info(
        "modal_call_cost",
        backend=cost.backend,
        gpu=cost.gpu_type,
        wall_seconds=round(cost.wall_seconds, 2),
        cost_usd=round(cost.cost_usd, 6),
        billed_delta_usd=round(cost.billed_delta_usd, 6),
        cost_source=cost.cost_source,
        collection=context.get("collection", ""),
        run_id=context.get("run_id", ""),
    )


async def measure(
    awaitable: Awaitable[T],
    *,
    backend: str,
) -> tuple[T, CallCost | None]:
    """Run a Modal awaitable and measure its cost.

    Args:
        awaitable: The pending Modal call.
        backend: Backend serving the call; selects the GPU for pricing.

    Returns:
        A ``(result, cost)`` tuple. ``cost`` is ``None`` when cost tracking is
        disabled.
    """
    started = time.monotonic()
    result = await awaitable
    wall_seconds = time.monotonic() - started

    if not settings.modal_cost_tracking_enabled:
        return result, None

    card = await rate_card()
    gpu = BACKEND_GPU.get(backend)
    cost_usd = compute_cost_usd(wall_seconds=wall_seconds, gpu=gpu, card=card)
    cost = CallCost(
        backend=backend,
        gpu_type=gpu,
        wall_seconds=wall_seconds,
        gpu_seconds=wall_seconds if gpu else 0.0,
        cost_usd=cost_usd,
        billed_delta_usd=0.0,
    )
    await record_call(cost)
    return result, cost


@asynccontextmanager
async def episode_tags(collection: str, run_id: str = "") -> AsyncIterator[None]:
    """Tag the Modal app for the duration of an episode, then restore.

    ``modal billing report --tag-names`` reads the tag values in use during each
    interval, so this attributes the episode's Modal cost to its collection and
    run. Best-effort: when tagging is disabled or fails, the block still runs
    untagged.

    Args:
        collection: Collection the episode belongs to.
        run_id: Run id the episode belongs to.

    Yields:
        ``None`` while the tags are set.
    """
    if not settings.modal_enabled or not settings.modal_tags_enabled:
        yield
        return
    tags = {"project": settings.modal_project_tag, "collection": collection}
    if run_id:
        tags["run_id"] = run_id
    previous = await _apply_tags(tags)
    if previous is None:
        yield
        return
    try:
        yield
    finally:
        await _apply_tags(previous)


async def _apply_tags(tags: dict[str, str]) -> dict[str, str] | None:
    """Set the Modal app tags and return the previous set.

    Args:
        tags: Complete tag set to store (Modal replaces, it does not merge).

    Returns:
        The previous tag set, or ``None`` when tagging failed.
    """
    global _APP
    try:
        if _APP is None:
            from modal.app import _App

            _APP = await _App.lookup(settings.modal_app_name)
        previous = await _APP.get_tags()
        await _APP.set_tags(tags)
        return dict(previous)
    except Exception as exc:  # noqa: BLE001 - best-effort attribution
        logger.warning("modal_tags_failed", tags=tags, error=str(exc))
        _APP = None
        return None
