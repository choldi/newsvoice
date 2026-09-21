"""System-monitor checks.

Each check is an ``async`` function taking a :class:`CheckContext` and returning
a :class:`CheckResult`. Adding a new monitor is just: write a function and append
it to :data:`MONITOR_CHECKS` — no workflow, activity, or schedule changes needed.

Checks are run inside a single Temporal activity (``run_monitor_checks_activity``)
so imports, HTTP probes, and DB reads stay out of the deterministic workflow.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import structlog

from src.api.db.database import RunsRepository
from src.api.models.monitoring import CheckResult
from src.api.services.monitoring.docker_status import DockerNotAvailableError, DockerService
from src.core.config import Settings, get_settings

if TYPE_CHECKING:
    from src.api.services.temporal_admin import TemporalAdminService

logger = structlog.get_logger(__name__)

FEED_PROBE_TIMEOUT = 10.0
SERVICE_PROBE_TIMEOUT = 5.0
_RUNS_MISSED_CUTOFF_HOURS = 36


def _default_temporal_admin() -> TemporalAdminService:
    """Build a TemporalAdminService, imported lazily to avoid an import cycle.

    Returns:
        A connected TemporalAdminService instance.
    """
    from src.api.services.temporal_admin import TemporalAdminService

    return TemporalAdminService()


@dataclass
class CheckContext:
    """Shared dependencies injected into every check (override in tests)."""

    settings: Settings = field(default_factory=get_settings)
    temporal_admin: TemporalAdminService = field(default_factory=_default_temporal_admin)
    runs_repo: RunsRepository = field(default_factory=RunsRepository)
    docker: DockerService = field(default_factory=DockerService)


CheckFn = Callable[[CheckContext], Awaitable[CheckResult]]


def _human_size(num: int) -> str:
    """Format a byte count into a short human-readable string.

    Args:
        num: Byte count.

    Returns:
        String like "1.2G" or "340M".
    """
    value = float(num)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}T"


async def check_docker_containers(ctx: CheckContext) -> CheckResult:
    """Report containers that are stopped, restarting, or unhealthy.

    Args:
        ctx: Shared check dependencies.

    Returns:
        A CheckResult with status 'skipped' when the socket is absent,
        'warning' when a container is not healthy, else 'ok'.
    """
    if not ctx.docker.enabled:
        return CheckResult(
            check="docker_containers",
            status="skipped",
            detail="Docker socket not configured (MONITOR_DOCKER_SOCKET empty)",
        )
    try:
        unhealthy = await ctx.docker.unhealthy()
    except DockerNotAvailableError as exc:
        return CheckResult(
            check="docker_containers",
            status="skipped",
            detail=f"Docker socket unavailable: {exc}",
        )
    if not unhealthy:
        return CheckResult(check="docker_containers", status="ok", detail="all containers healthy")
    names = [", ".join(c.get("names", []) or c.get("id", "?")) for c in unhealthy]
    return CheckResult(
        check="docker_containers",
        status="warning",
        detail=f"containers not running/healthy: {'; '.join(names)}",
    )


async def check_edge_tts(ctx: CheckContext) -> CheckResult:
    """Probe the edge-tts (Microsoft Edge) synthesis backend.

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult 'ok' when the voice catalogue is reachable within the
        timeout, 'error' otherwise.
    """
    import edge_tts

    try:
        await asyncio.wait_for(edge_tts.list_voices(), timeout=15.0)
    except Exception as exc:
        return CheckResult(
            check="edge_tts", status="error", detail=f"edge-tts backend unreachable: {exc}"
        )
    return CheckResult(check="edge_tts", status="ok", detail="edge-tts voices reachable")


async def check_tts_server_disk(ctx: CheckContext) -> CheckResult:
    """Probe the TTS server's disk endpoint.

    Expects ``GET <tts_api_base_url>/<monitor_tts_disk_path>`` to return
    ``{"total": int, "free": int, "used": int}`` (bytes).

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult 'ok', 'warning' (free below threshold), or 'skipped' when
        the TTS server does not expose the endpoint.
    """
    base = (ctx.settings.tts_api_base_url or "").rstrip("/")
    url = f"{base}/{ctx.settings.monitor_tts_disk_path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return CheckResult(
            check="tts_server_disk",
            status="skipped",
            detail=f"TTS disk endpoint unavailable: {exc}",
        )
    try:
        total = int(data["total"])
        free = int(data["free"])
    except (KeyError, TypeError, ValueError) as exc:
        return CheckResult(
            check="tts_server_disk",
            status="skipped",
            detail=f"TTS disk payload malformed ({exc}): {data}",
        )
    pct_free = free / total * 100 if total else 100.0
    if pct_free < ctx.settings.monitor_disk_threshold_percent:
        return CheckResult(
            check="tts_server_disk",
            status="warning",
            detail=f"TTS server {pct_free:.1f}% free ({_human_size(free)} of {_human_size(total)})",
        )
    return CheckResult(
        check="tts_server_disk",
        status="ok",
        detail=f"TTS server {pct_free:.1f}% free ({_human_size(free)} of {_human_size(total)})",
    )


async def _probe(url: str, timeout: float = SERVICE_PROBE_TIMEOUT) -> str | None:
    """Return None on success or a short failure string.

    Non-JSON 2xx responses (e.g. the WhatsApp gateway's QR page) count as
    healthy; JSON bodies with a ``status`` other than 'ok' do not.

    Args:
        url: URL to GET.
        timeout: Timeout in seconds.

    Returns:
        None if the probe succeeded, else an error description.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    return parse_service_probe(url, response.json())
                except ValueError:
                    return None
            return None
    except Exception as exc:
        return str(exc)


def _probe_status(data: dict) -> str:
    """Read the JSON status field, tolerating missing keys."""
    if "status" in data:
        return str(data["status"])
    return "ok" if isinstance(data, dict) and data else "up"


def parse_service_probe(url: str, payload: dict) -> str | None:
    """Validate a generic {status: ok} service probe response.

    Args:
        url: Origin of the payload (used to format errors).
        payload: Parsed JSON body.

    Returns:
        None when it looks healthy, else an error string.
    """
    status = _probe_status(payload)
    if status != "ok":
        return f"{url} -> status {status!r}"
    return None


async def check_services(ctx: CheckContext) -> CheckResult:
    """Probe the internal compose services over HTTP.

    Checks the api (/health) always, plus the WhatsApp gateway (there) when
    configured. Temporal itself is gRPC-only (no HTTP health endpoint), so its
    liveness comes from the ``docker_containers`` check.

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult listing any unreachable or unhealthy service.
    """
    probes: dict[str, Callable[[], Awaitable[str | None]]] = {
        "api": lambda: _probe("http://api:8000/health"),
    }
    settings = ctx.settings
    if settings.whatsapp_api_token:
        probes["whatsapp_gateway"] = lambda: _probe("http://there:8085/")

    problems: list[str] = []
    results = await asyncio.gather(*(fn() for fn in probes.values()))
    for name, error in zip(probes, results):
        if error is not None:
            problems.append(f"{name}: {error}")

    if problems:
        return CheckResult(check="services", status="warning", detail="; ".join(problems))
    return CheckResult(check="services", status="ok", detail="api, gateway reachable")


async def check_collections(ctx: CheckContext) -> CheckResult:
    """Validate collections.toml, its feed URLs, and the Temporal schedules.

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult 'ok' when the file parses, every feed responds, and a
        ``daily-digest-<name>`` schedule exists for every enabled collection.
    """
    from src.core.collections import CollectionsLoader, default_collection

    settings = ctx.settings
    loader = CollectionsLoader(path=settings.collections_file)
    try:
        loader.load()
    except Exception as exc:
        return CheckResult(
            check="collections",
            status="error",
            detail=f"collections.toml parse failed: {exc}",
        )

    collections = list(loader.enabled()) or [default_collection()]
    issues: list[str] = []
    feed_tasks: list[tuple[str, str]] = [
        (collection.name, url) for collection in collections for url in collection.feed_urls
    ]
    feed_results = await asyncio.gather(
        *(_probe(url, timeout=FEED_PROBE_TIMEOUT) for _, url in feed_tasks)
    )
    for (name, url), error in zip(feed_tasks, feed_results):
        if error is not None:
            issues.append(f"{name} feed {url}: {error}")

    expected_ids = {f"daily-digest-{c.name}" for c in collections}
    missing: list[str] = []
    try:
        existing = set(await ctx.temporal_admin.list_schedule_ids())
        missing = sorted(expected_ids - existing)
    except Exception as exc:
        issues.append(f"schedules unreachable: {exc}")

    if missing:
        issues.append(f"missing schedules: {', '.join(missing)}")

    if issues:
        return CheckResult(check="collections", status="warning", detail="; ".join(issues))
    return CheckResult(
        check="collections",
        status="ok",
        detail=f"{len(collections)} collections, {len(feed_tasks)} feeds, schedules present",
    )


async def check_digest_runs(ctx: CheckContext) -> CheckResult:
    """Report the digest run health per collection.

    Flags collections whose latest run failed or that have not run recently
    (missed the daily schedule).

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult with a per-collection summary or 'ok' when everything ran
        cleanly.
    """
    from src.core.collections import CollectionsLoader, default_collection

    settings = ctx.settings
    loader = CollectionsLoader(path=settings.collections_file)
    try:
        loader.load()
    except Exception:
        return CheckResult(
            check="digest_runs", status="error", detail="collections.toml unavailable"
        )
    collections = list(loader.enabled()) or [default_collection()]
    cutoff = datetime.now(UTC) - timedelta(hours=_RUNS_MISSED_CUTOFF_HOURS)

    lines: list[str] = []
    problems: list[str] = []
    for collection in collections:
        runs = await ctx.runs_repo.list_runs(collection=collection.name, limit=1)
        if not runs:
            problems.append(f"{collection.name}: never run")
            lines.append(f"{collection.name}: never run")
            continue
        last = runs[0]
        last_ts = last.get("completed_at") or last.get("started_at") or ""
        summary = (
            f"{collection.name}: {last.get('status', '?')} "
            f"({last.get('article_count', 0)} articles, "
            f"audio={'y' if last.get('audio_path') else 'n'}, {last_ts[:16]})"
        )
        lines.append(summary)
        if last.get("status") == "failed":
            problems.append(
                f"{collection.name}: last run failed: {last.get('error') or 'no error detail'}"
            )
        elif last_ts < cutoff.isoformat():
            problems.append(f"{collection.name}: no run in the last {_RUNS_MISSED_CUTOFF_HOURS}h")

    status = "warning" if problems else "ok"
    return CheckResult(
        check="digest_runs",
        status=status,
        detail="; ".join(lines) if problems else "; ".join(lines) or "no collections",
    )


async def check_disk_local(ctx: CheckContext) -> CheckResult:
    """Report the free space of the host filesystem backing ``data/``.

    Args:
        ctx: Shared check dependencies.

    Returns:
        CheckResult 'ok' or 'warning' when free space drops below the threshold.
    """
    data_dir = Path(ctx.settings.database_path).parent
    cache_dir = Path(ctx.settings.tts_cache_dir)
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as exc:
        return CheckResult(check="disk_local", status="error", detail=f"disk_usage failed: {exc}")
    pct_free = usage.free / usage.total * 100 if usage.total else 100.0
    cache_size = 0
    if cache_dir.exists():
        cache_size = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file())

    detail = (
        f"{pct_free:.1f}% free ({_human_size(usage.free)} of {_human_size(usage.total)}); "
        f"tts_cache {_human_size(cache_size)}"
    )
    if pct_free < ctx.settings.monitor_disk_threshold_percent:
        return CheckResult(
            check="disk_local",
            status="warning",
            detail=f"{detail} — below {ctx.settings.monitor_disk_threshold_percent}%",
        )
    return CheckResult(check="disk_local", status="ok", detail=detail)


# Registry: append a check function here to include it in every monitor run.
MONITOR_CHECKS: list[tuple[str, CheckFn]] = [
    ("docker_containers", check_docker_containers),
    ("edge_tts", check_edge_tts),
    ("tts_server_disk", check_tts_server_disk),
    ("services", check_services),
    ("collections", check_collections),
    ("digest_runs", check_digest_runs),
    ("disk_local", check_disk_local),
]


async def run_all_checks(ctx: CheckContext | None = None) -> list[CheckResult]:
    """Run every registered check and return its outcomes in registry order.

    Args:
        ctx: Shared check dependencies; defaults to a real CheckContext.

    Returns:
        List of CheckResult, one per entry in MONITOR_CHECKS.
    """
    context = ctx or CheckContext()
    results = await asyncio.gather(
        *(fn(context) for _, fn in MONITOR_CHECKS),
        return_exceptions=True,
    )
    return [
        result if isinstance(result, CheckResult) else _failed_result(name, result)
        for (name, _), result in zip(MONITOR_CHECKS, results)
    ]


def _failed_result(name: str, error: BaseException | None) -> CheckResult:
    """Wrap an unhandled check failure into an error CheckResult.

    Args:
        name: Check name.
        error: The raised exception, if any.

    Returns:
        A CheckResult with status 'error'.
    """
    return CheckResult(check=name, status="error", detail=f"check raised: {error}")
