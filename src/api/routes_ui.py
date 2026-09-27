"""Admin web GUI routes (Jinja2 + HTMX) for Phase 6.

Self-hosted single-page-ish admin console served under /ui. The GUI renders
collections, schedule calendar, executions (merged Temporal + DB runs), and
voice management. It uses the admin REST services directly (no HTTP hop) and
HTMX for live partial refreshes (executions polling, calendar, tables).

Auth is handled by the ADMIN_API_TOKEN cookie middleware (`src/api/auth.py`).

HTML form bodies are parsed with the stdlib (`urllib.parse.parse_qsl`) instead of
`python-multipart`, so the GUI needs no extra runtime dependency (forms are
`application/x-www-form-urlencoded`; file uploads are not used).
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.api.db.database import AudioJobsRepository, RunsRepository
from src.api.models.narration import NarrationSpec
from src.api.services.collections_repo import CollectionConflictError, CollectionsRepository
from src.api.services.temporal_admin import TemporalAdminService
from src.api.services.voices import VoicesService
from src.core.collections import LogicDeliveryConfig

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory="src/api/templates")

WEEKDAY_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


async def _form(request: Request) -> dict[str, str]:
    """Parse an `application/x-www-form-urlencoded` body into a string dict.

    Uses the stdlib to avoid a python-multipart runtime dependency. Checkbox
    fields absent from the request simply won't appear as keys.

    Args:
        request: Incoming request.

    Returns:
        Dict of form field name to its first string value.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for key, value in parse_qsl(raw, keep_blank_values=True):
        fields.setdefault(key, value)
    return fields


STATUS_LABELS = {
    "ok": "ok",
    "completed": "ok",
    "no_new_articles": "sin novedades",
    "failed": "error",
    "running": "en ejecución",
    "terminated": "parado",
    "canceled": "cancelado",
    "timed_out": "tiempo agotado",
    "continued_as_new": "continúa como nuevo",
}


def _collections() -> list:
    """Return all collections from collections.toml."""
    return CollectionsRepository().list()


async def _omni_options() -> dict:
    """Fetch OmniVoice languages and voices for the TTS dropdowns.

    Returns:
        Dict with 'languages' (sorted unique codes) and 'voices' (stock +
        cloned). Empty lists if OmniVoice is unreachable.
    """
    service = VoicesService()
    try:
        languages = await service.list_languages()
        voices = await service.list_stock() + await service.list_cloned()
    except Exception as exc:
        logger.warning("omni_options_failed", error=str(exc))
        return {"languages": [], "voices": []}
    return {"languages": languages, "voices": voices}


def _split_list(raw: str) -> list[str]:
    """Split a textarea value into a cleaned, de-duplicated list.

    Accepts newline- or comma-separated values; blank entries are dropped and
    the original order is preserved. Format validation (E.164, email shape) is
    left to the Pydantic models.

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed values.
    """
    seen: list[str] = []
    for item in raw.replace(",", "\n").splitlines():
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _split_emails(raw: str) -> list[str]:
    """Split a form value into a cleaned list of email recipients.

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed addresses.
    """
    return _split_list(raw)


def _split_whatsapp(raw: str) -> list[str]:
    """Split a form value into a cleaned list of WhatsApp numbers (E.164).

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed numbers. Format validation happens in
        `CollectionConfig`.
    """
    return _split_list(raw)


DELIVERY_CHANNELS = ("telegram", "email", "whatsapp")
LOGIC_CHANNELS = ("telegram", "email", "whatsapp")
LOGIC_FORM_KEYS = (
    "logic_chat_id",
    "logic_email_recipients",
    "logic_whatsapp_recipients",
    "logic_whatsapp_group_id",
)


async def _checked_values(request: Request, field: str, allowed: tuple[str, ...]) -> list[str]:
    """Extract the selected checkboxes of a form body.

    Args:
        request: Incoming form request.
        field: Checkbox input name.
        allowed: Accepted values, in canonical order.

    Returns:
        Ordered, de-duplicated list of checked values.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    values = [
        value
        for key, value in parse_qsl(raw, keep_blank_values=True)
        if key == field and value in allowed
    ]
    return list(dict.fromkeys(values))


async def _selected_channels(request: Request) -> list[str]:
    """Extract the delivery channel checkboxes from a form body.

    Values are 'telegram', 'email' and 'whatsapp'; an empty selection (no
    checkbox ticked) falls back to the default telegram-only delivery.

    Args:
        request: Incoming form request.

    Returns:
        Ordered, validated list of selected delivery channels.
    """
    return await _checked_values(request, "delivery_channels", DELIVERY_CHANNELS) or ["telegram"]


def _logic_delivery_from_form(
    fields: dict[str, str],
    channels: list[str],
    current: LogicDeliveryConfig | None,
) -> LogicDeliveryConfig | None:
    """Build the optional `logic_delivery` block from form fields.

    When the block is absent from the body (legacy or programmatic clients) the
    current value is preserved, so an update never erases the configuration. When
    the block is present but completely empty the destination is disabled.

    Args:
        fields: Parsed form fields.
        channels: Checked logic delivery channels.
        current: Existing configuration to preserve when the block is absent.

    Returns:
        The logic delivery configuration, or None when disabled.
    """
    if not channels and not any(key in fields for key in LOGIC_FORM_KEYS):
        return current
    chat_id = fields.get("logic_chat_id", "").strip() or None
    email_recipients = _split_emails(fields.get("logic_email_recipients", ""))
    whatsapp_recipients = _split_whatsapp(fields.get("logic_whatsapp_recipients", ""))
    group_id = fields.get("logic_whatsapp_group_id", "").strip() or None
    if not (channels or chat_id or email_recipients or whatsapp_recipients or group_id):
        return None
    return LogicDeliveryConfig(
        channels=channels,
        chat_id=chat_id,
        email_recipients=email_recipients,
        whatsapp_recipients=whatsapp_recipients,
        whatsapp_group_id=group_id,
    )


def _flash(redirect_url: str, message: str, level: str = "ok") -> RedirectResponse:
    """Redirect with a flash message attached as a query parameter.

    Args:
        redirect_url: Target page URL.
        message: Message to show to the user.
        level: Flash level ('ok', 'warn' or 'error').

    Returns:
        A RedirectResponse carrying the flash in its query string.
    """
    import urllib.parse

    sep = "&" if "?" in redirect_url else "?"
    redirect = f"{redirect_url}{sep}flash={urllib.parse.quote(message)}&level={level}"
    return RedirectResponse(redirect, 302)


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    """Render a template with the base context.

    Args:
        request: Incoming request (for Jinja2 URLs).
        name: Template file name.
        context: Page-specific context dict.

    Returns:
        An HTMLResponse.
    """
    context.setdefault("request", request)
    context.setdefault("flash", request.query_params.get("flash", ""))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    context.setdefault("active", name.split(".")[0])
    return templates.TemplateResponse(request, name, context)


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse an HH:MM collection time into (hour, minute).

    Args:
        time_str: Time in HH:MM format.

    Returns:
        (hour, minute) tuple; falls back to (8, 0) on bad input.
    """
    try:
        hour, minute = (int(part) for part in time_str.split(":"))
        return hour, minute
    except (TypeError, ValueError):
        return 8, 0


def _week_grid(collections: list) -> list[dict]:
    """Build a 7-day weekly grid marking collection schedule times.

    Args:
        collections: List of collection configs.

    Returns:
        List of dicts keyed by weekday index with the scheduled collections.
    """
    grid: list[dict] = [{"name": name, "slots": []} for name in WEEKDAY_NAMES]
    for collection in collections:
        if not collection.enabled:
            continue
        hour, minute = _parse_time(collection.time)
        for day in range(7):
            grid[day]["slots"].append({"name": collection.name, "hour": hour, "minute": minute})
    for day in grid:
        day["slots"].sort(key=lambda s: (s["hour"], s["minute"]))
    return grid


@router.get("/ui", include_in_schema=False)
async def ui_home() -> RedirectResponse:
    """Redirect the /ui root to the collections page."""
    return RedirectResponse("/ui/collections")


@router.get("/ui/collections", response_class=HTMLResponse, include_in_schema=False)
async def ui_collections(request: Request) -> HTMLResponse:
    """Render the collections management page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the collections page.
    """
    collections = _collections()
    omni = (
        await _omni_options()
        if any(c.tts_enabled for c in collections)
        else {
            "languages": [],
            "voices": [],
        }
    )
    return _render(request, "collections.html", {"collections": collections, "omni": omni})


@router.get("/ui/calendar", response_class=HTMLResponse, include_in_schema=False)
async def ui_calendar(request: Request) -> HTMLResponse:
    """Render the weekly schedule calendar page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the calendar page.
    """
    collections = _collections()
    today = datetime.now(UTC).date()
    week_start = today - timedelta(days=today.weekday())
    return _render(
        request,
        "calendar.html",
        {
            "collections": _week_grid(collections),
            "week_start": week_start,
            "today": today,
            "hour_range": range(24),
            "weekday_names": WEEKDAY_NAMES,
        },
    )


def _collection_from_workflow_id(workflow_id: str, known: list[str]) -> str:
    """Best-effort parse of the collection name from a digest workflow id.

    Matches the longest known collection name that prefixes the id suffix;
    falls back to stripping the trailing timestamp.

    Args:
        workflow_id: Temporal workflow id ('digest-pipeline-<name>-<ts>').
        known: Known collection names.

    Returns:
        The inferred collection name.
    """
    suffix = workflow_id.removeprefix("digest-pipeline-")
    for name in sorted(known, key=len, reverse=True):
        if suffix == name or suffix.startswith(f"{name}-"):
            return name
    return suffix.rsplit("-", 1)[0]


def _fmt_utc(value: datetime | None) -> str:
    """Format a UTC datetime for display.

    Args:
        value: Datetime or None.

    Returns:
        'YYYY-MM-DD HH:MM UTC' or an empty string when None.
    """
    if value is None:
        return ""
    return f"{value.strftime('%Y-%m-%d %H:%M')} UTC"


def _fmt_iso(value: str | None) -> str:
    """Format an ISO timestamp string (as stored in the DB) for display.

    Args:
        value: ISO string like '2026-09-14T08:00:00+00:00' or None.

    Returns:
        'YYYY-MM-DD HH:MM UTC' or an empty string when None.
    """
    if not value:
        return ""
    return f"{value[:16].replace('T', ' ')} UTC"


async def _executions(limit: int = 50) -> list[dict]:
    """Merge Temporal visibility executions with the DB run history.

    Inventory of the digest pipeline's executions: Temporal visibility provides
    the real status (running/completed/failed/terminated/...) while the DB adds
    article/cluster counts, digest preview and stored error messages.

    Args:
        limit: Maximum number of Temporal executions to fetch.

    Returns:
        Merged list of run dicts, newest first.
    """
    service = TemporalAdminService()
    known = [c.name for c in _collections()]
    try:
        execs = await service.list_executions(limit=limit)
    except Exception as exc:
        logger.warning("list_executions_failed", error=str(exc))
        execs = []

    db_runs = await RunsRepository().list_runs(limit=300)
    db_by_wf = {r["workflow_id"]: r for r in db_runs}

    merged: list[dict] = []
    seen: set[str] = set()
    for exec in execs:
        wf = exec["workflow_id"]
        seen.add(wf)
        db = db_by_wf.get(wf, {})
        merged.append(
            {
                "workflow_id": wf,
                "collection": db.get("collection") or _collection_from_workflow_id(wf, known),
                "status": exec["status"],
                "start_label": _fmt_utc(exec["start_time"]),
                "end_label": _fmt_utc(exec["close_time"]),
                "article_count": db.get("article_count"),
                "cluster_count": db.get("cluster_count"),
                "digest_preview": db.get("digest_preview"),
                "error": db.get("error"),
                "run_id": db.get("id"),
            }
        )
    for wf, db in db_by_wf.items():
        if wf in seen:
            continue
        merged.append(
            {
                "workflow_id": wf,
                "collection": db.get("collection"),
                "status": db["status"],
                "start_label": _fmt_iso(db.get("started_at")),
                "end_label": _fmt_iso(db.get("completed_at")),
                "article_count": db.get("article_count"),
                "cluster_count": db.get("cluster_count"),
                "digest_preview": db.get("digest_preview"),
                "error": db.get("error"),
                "run_id": db.get("id"),
            }
        )
    merged.sort(key=lambda r: r["start_label"], reverse=True)
    return merged


@router.get("/ui/runs", response_class=HTMLResponse, include_in_schema=False)
async def ui_runs(request: Request) -> HTMLResponse:
    """Render the executions page (merged Temporal + DB runs).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the executions page.
    """
    return _render(request, "runs.html", {})


@router.get("/ui/voices", response_class=HTMLResponse, include_in_schema=False)
async def ui_voices(request: Request) -> HTMLResponse:
    """Render the voices management page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the voices page.
    """
    service = VoicesService()
    stock = await service.list_stock()
    cloned = await service.list_cloned()
    return _render(request, "voices.html", {"stock": stock, "cloned": cloned})


@router.get("/ui/fragments/runs", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_runs(request: Request) -> HTMLResponse:
    """Render the executions table fragment for HTMX polling.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the executions table body.
    """
    runs = await _executions()
    return _render(
        request,
        "partials/_executions.html",
        {"runs": runs, "status_labels": STATUS_LABELS},
    )


@router.get("/ui/fragments/run_detail", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_run_detail(
    request: Request, workflow_id: str, run_id: int | None = None
) -> HTMLResponse:
    """Render the detail panel for a single execution.

    Args:
        request: Incoming request.
        workflow_id: Temporal workflow id.
        run_id: Optional DB row id.

    Returns:
        HTMLResponse with the run detail partial.
    """
    detail: dict = {
        "error": None,
        "digest_preview": None,
        "article_count": None,
        "cluster_count": None,
        "narration_path": None,
        "narration_job": None,
    }
    if run_id:
        row = await RunsRepository().get_run(run_id)
        if row:
            detail = {**detail, **row}
            if row.get("narration_path"):
                detail["narration_path"] = row["narration_path"]
            # Fetch narration audio job
            job = await AudioJobsRepository().get_job(workflow_id, kind="narration")
            if job:
                detail["narration_job"] = job
    if not detail.get("error"):
        try:
            detail["error"] = await TemporalAdminService().execution_error(workflow_id)
        except Exception as exc:
            logger.warning("execution_error_failed", workflow_id=workflow_id, error=str(exc))
    return _render(request, "partials/_run_detail.html", {"workflow_id": workflow_id, **detail})


@router.post("/ui/runs/{workflow_id}/terminate", include_in_schema=False)
async def ui_terminate_run(workflow_id: str) -> RedirectResponse:
    """Terminate a running execution from the GUI.

    Args:
        workflow_id: Temporal workflow id to terminate.

    Returns:
        Redirect to the executions page with a flash.
    """
    try:
        await TemporalAdminService().terminate_workflow(workflow_id)
    except Exception as exc:
        logger.error("ui_terminate_failed", workflow_id=workflow_id, error=str(exc), exc_info=True)
        return _flash("/ui/runs", f"Fallo al detener: {exc}", "error")
    return _flash("/ui/runs", f"Ejecución '{workflow_id}' detenida")


@router.get("/ui/fragments/tts_options", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_tts_options(
    request: Request, tts_enabled: str = "", language: str = "", voice: str = ""
) -> HTMLResponse:
    """Render the language/voice fields for a collection form (TTS on/off).

    When ``tts_enabled`` is truthy and OmniVoice is reachable, renders selects
    populated with real voices; otherwise renders free-text inputs. Used by the
    collections form via an HTMX swap on the TTS checkbox.

    Args:
        request: Incoming request.
        tts_enabled: 'on' when TTS is enabled for the collection.
        language: Current language value to keep selected.
        voice: Current voice value to keep selected.

    Returns:
        HTMLResponse with the _tts_options partial.
    """
    omni = await _omni_options() if tts_enabled else {"languages": [], "voices": []}
    return _render(
        request,
        "partials/_tts_options.html",
        {
            "tts_enabled": bool(tts_enabled),
            "omni_languages": omni["languages"],
            "omni_voices": omni["voices"],
            "current_language": language,
            "current_voice": voice,
        },
    )


@router.get("/ui/fragments/calendar", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_calendar(request: Request) -> HTMLResponse:
    """Render the calendar body fragment.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the calendar grid body.
    """
    today = datetime.now(UTC).date()
    week_start = today - timedelta(days=today.weekday())
    return _render(
        request,
        "partials/_calendar.html",
        {
            "collections": _week_grid(_collections()),
            "week_start": week_start,
            "today": today,
            "hour_range": range(24),
            "weekday_names": WEEKDAY_NAMES,
        },
    )


def _narration_spec_from_form(fields: dict[str, str]) -> NarrationSpec | None:
    """Parse narration_spec from form data.

    The form sends it as a JSON string in a textarea. Empty or invalid JSON returns None.

    Args:
        fields: Form field dict.

    Returns:
        Parsed NarrationSpec or None.
    """
    raw = fields.get("narration_spec", "").strip()
    if not raw:
        return None
    try:
        return NarrationSpec.model_validate_json(raw)
    except Exception:
        return None


@router.post("/ui/collections", include_in_schema=False)
async def ui_create_collection(request: Request) -> RedirectResponse:
    """Create a collection from the GUI form.

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the collections page with a flash.
    """
    from src.api.models.admin import CollectionIn

    fields = await _form(request)
    name = fields.get("name", "").strip()
    if not name:
        return _flash("/ui/collections", "Nombre de colección requerido", "error")
    urls = [
        u.strip() for u in fields.get("feed_urls", "").replace(",", "\n").splitlines() if u.strip()
    ]
    try:
        config = CollectionIn(
            name=name,
            time=fields.get("time", "08:00"),
            feed_urls=urls,
            tts_enabled=fields.get("tts_enabled") == "on",
            deliver_audio=fields.get("deliver_audio") == "on",
            podcast_name=fields.get("podcast_name", ""),
            podcast_behavior=fields.get("podcast_behavior", ""),
            language=fields.get("language", ""),
            voice=fields.get("voice", ""),
            prompt_template=fields.get("prompt_template") or "default",
            custom_prompt=fields.get("custom_prompt", ""),
            chat_id=fields.get("chat_id") or None,
            email_recipients=_split_emails(fields.get("email_recipients", "")),
            whatsapp_recipients=_split_whatsapp(fields.get("whatsapp_recipients", "")),
            whatsapp_group_id=fields.get("whatsapp_group_id", "").strip() or None,
            delivery_channels=await _selected_channels(request),
            logic_delivery=_logic_delivery_from_form(
                fields, await _checked_values(request, "logic_channels", LOGIC_CHANNELS), None
            ),
            narration_enabled=fields.get("narration_enabled") == "on",
            narration_spec=_narration_spec_from_form(fields),
            narration_strategy=fields.get("narration_strategy", "turns"),
            narration_deliver_audio=fields.get("narration_deliver_audio") != "off",
            main_story_count=int(fields.get("main_story_count") or 5),
            headline_count=int(fields.get("headline_count") or 5),
        ).to_config()
        CollectionsRepository().create(config)
    except CollectionConflictError as exc:
        return _flash("/ui/collections", str(exc), "error")
    except Exception as exc:
        return _flash("/ui/collections", f"Error al crear: {exc}", "error")
    return _flash("/ui/collections", f"Colección '{name}' creada")


@router.post("/ui/collections/{name}/update", include_in_schema=False)
async def ui_update_collection(name: str, request: Request) -> RedirectResponse:
    """Update a collection from the GUI form.

    Args:
        name: Current collection name.
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the collections page with a flash.
    """
    from src.api.models.admin import CollectionIn

    fields = await _form(request)
    new_name = fields.get("new_name", name).strip()
    repo = CollectionsRepository()
    current = repo.get(name)
    if current is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    urls = [
        u.strip() for u in fields.get("feed_urls", "").replace(",", "\n").splitlines() if u.strip()
    ]
    try:
        config = CollectionIn(
            name=new_name,
            time=fields.get("time", "08:00"),
            feed_urls=urls,
            tts_enabled=fields.get("tts_enabled") == "on",
            deliver_audio=fields.get("deliver_audio") == "on",
            podcast_name=fields.get("podcast_name", ""),
            podcast_behavior=fields.get("podcast_behavior", ""),
            language=fields.get("language", ""),
            voice=fields.get("voice", ""),
            prompt_template=fields.get("prompt_template") or "default",
            custom_prompt=fields.get("custom_prompt", ""),
            chat_id=fields.get("chat_id") or None,
            email_recipients=_split_emails(fields.get("email_recipients", "")),
            whatsapp_recipients=_split_whatsapp(fields.get("whatsapp_recipients", "")),
            whatsapp_group_id=fields.get("whatsapp_group_id", "").strip() or None,
            delivery_channels=await _selected_channels(request),
            logic_delivery=_logic_delivery_from_form(
                fields,
                await _checked_values(request, "logic_channels", LOGIC_CHANNELS),
                current.logic_delivery,
            ),
            narration_enabled=fields.get("narration_enabled") == "on",
            narration_spec=_narration_spec_from_form(fields),
            narration_strategy=fields.get("narration_strategy", "turns"),
            narration_deliver_audio=fields.get("narration_deliver_audio") != "off",
            main_story_count=int(fields.get("main_story_count") or 5),
            headline_count=int(fields.get("headline_count") or 5),
            enabled=fields.get("enabled") == "on",
        ).to_config()
        collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
        collections.append(config)
        repo.save_all(collections)
    except Exception as exc:
        return _flash("/ui/collections", f"Error al actualizar: {exc}", "error")
    return _flash("/ui/collections", f"Colección '{new_name}' actualizada")


@router.post("/ui/collections/{name}/delete", include_in_schema=False)
async def ui_delete_collection(name: str) -> RedirectResponse:
    """Delete a collection via the GUI.

    Args:
        name: Collection name.

    Returns:
        Redirect to the collections page with a flash.
    """
    repo = CollectionsRepository()
    if repo.get(name) is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
    repo.save_all(collections)
    return _flash("/ui/collections", f"Colección '{name}' eliminada")


@router.post("/ui/collections/{name}/run", include_in_schema=False)
async def ui_run_collection(name: str) -> RedirectResponse:
    """Trigger a collection run immediately.

    If a workflow for the collection is already running, does not start a new
    one and flashes a notice instead.

    Args:
        name: Collection name.

    Returns:
        Redirect to the collections page with a flash.
    """
    repo = CollectionsRepository()
    collection = repo.get(name)
    if collection is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    service = TemporalAdminService()
    try:
        running = await service.is_collection_running(collection.name)
    except Exception as exc:
        logger.error("ui_run_check_failed", collection=name, error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al comprobar la ejecución: {exc}", "error")
    if running:
        return _flash("/ui/collections", f"Ya hay una ejecución en curso para '{name}'", "warn")
    try:
        workflow_id = await service.trigger_collection(collection)
    except Exception as exc:
        logger.error("ui_run_failed", collection=name, error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al lanzar: {exc}", "error")
    return _flash("/ui/collections", f"Workflow lanzado: {workflow_id}")


@router.post("/ui/schedules/resync", include_in_schema=False)
async def ui_resync_schedules() -> RedirectResponse:
    """Resync Temporal schedules with collections.toml.

    Returns:
        Redirect to the collections page with a flash.
    """
    try:
        result = await TemporalAdminService().resync_schedules(CollectionsRepository().list())
        message = (
            f"Schedules resincronizados: {len(result['created_or_updated'])} creados/actualizados, "
            f"{len(result['removed'])} eliminados"
        )
    except Exception as exc:
        logger.error("ui_resync_failed", error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al resincronizar: {exc}", "error")
    return _flash("/ui/collections", message)


@router.post("/ui/voices/clone", include_in_schema=False)
async def ui_clone_voice(request: Request) -> RedirectResponse:
    """Clone a voice from a base64-encoded audio reference.

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the voices page with a flash.
    """
    import base64

    fields = await _form(request)
    name = fields.get("name", "").strip()
    audio_base64 = fields.get("audio_base64", "")
    try:
        payload = base64.b64decode(audio_base64, validate=True)
        await VoicesService().clone_voice(
            name,
            fields.get("language", "es"),
            payload,
            filename=fields.get("filename", "reference.wav"),
            ref_text=fields.get("ref_text", "").strip() or None,
        )
    except Exception as exc:
        return _flash("/ui/voices", f"Fallo al clonar voz: {exc}", "error")
    return _flash("/ui/voices", f"Voz '{name}' clonada")


@router.get("/ui/login", response_class=HTMLResponse, include_in_schema=False)
async def ui_login_page(request: Request) -> HTMLResponse:
    """Render the login page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the login page.
    """
    from src.core.config import settings

    use_legacy_token = bool(settings.admin_api_token) and not settings.admin_username
    return _render(request, "login.html", {"use_legacy_token": use_legacy_token})


@router.post("/ui/login", include_in_schema=False)
async def ui_login(request: Request) -> RedirectResponse:
    """Validate admin credentials and set the session cookie.

    Args:
        request: Incoming request with form fields.

    Returns:
        Redirect to /ui/collections on success, back to login on failure.
    """
    from src.api.auth import ADMIN_COOKIE_NAME, _password_ok, _token_ok
    from src.core.config import settings

    fields = await _form(request)

    # Username/password flow (preferred when ADMIN_USERNAME is set)
    if settings.admin_username:
        username = fields.get("username", "").strip()
        password = fields.get("password", "")
        if username != settings.admin_username or not _password_ok(password):
            return _flash("/ui/login", "Usuario o contraseña incorrectos", "error")
    else:
        # Legacy token flow
        token = fields.get("token", "").strip()
        expected = (settings.admin_api_token or "").strip()
        if not expected:
            return _flash(
                "/ui/login",
                "ADMIN_API_TOKEN no configurado. Añádelo al .env y reinicia.",
                "error",
            )
        if not _token_ok(token):
            return _flash("/ui/login", "Token incorrecto", "error")
        password = token  # store token in cookie for middleware

    response = RedirectResponse("/ui/collections", status_code=302)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        password,
        httponly=True,
        samesite="lax",
        max_age=12 * 60 * 60,
    )
    return response


@router.post("/ui/logout", include_in_schema=False)
async def ui_logout() -> RedirectResponse:
    """Clear the admin session cookie.

    Returns:
        Redirect to the login page.
    """
    from src.api.auth import ADMIN_COOKIE_NAME

    response = RedirectResponse("/ui/login", status_code=302)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response
