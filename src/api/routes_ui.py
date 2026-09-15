"""Admin web GUI routes (Jinja2 + HTMX) for Phase 6.

Self-hosted single-page-ish admin console served under /ui. The GUI renders
collections, schedule calendar, run history, errors, and voice management. It
uses the admin REST services directly (no HTTP hop) and HTMX for live partial
refreshes (run history polling, calendar, tables).

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

from src.api.db.database import RunsRepository
from src.api.services.collections_repo import CollectionConflictError, CollectionsRepository
from src.api.services.temporal_admin import TemporalAdminService
from src.api.services.voices import VoicesService

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
    "no_new_articles": "sin novedades",
    "failed": "error",
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


def _split_emails(raw: str) -> list[str]:
    """Split a form value into a cleaned list of email recipients.

    Accepts newline- or comma-separated addresses; blank entries are dropped.

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed addresses.
    """
    seen: list[str] = []
    for addr in raw.replace(",", "\n").splitlines():
        addr = addr.strip()
        if addr and addr not in seen:
            seen.append(addr)
    return seen


def _flash(redirect_url: str, message: str, level: str = "ok") -> RedirectResponse:
    """Redirect with a flash message attached as a query parameter.

    Args:
        redirect_url: Target page URL.
        message: Message to show to the user.
        level: Flash level ('ok' or 'error').

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


async def _runs_rows(
    collection: str | None = None, run_status: str | None = None, limit: int = 50
) -> list[dict]:
    """Fetch recent runs, newest first.

    Args:
        collection: Optional collection filter.
        run_status: Optional status filter.
        limit: Max rows.

    Returns:
        List of run dicts.
    """
    return await RunsRepository().list_runs(
        collection=collection, status_filter=run_status, limit=limit
    )


@router.get("/ui/history", response_class=HTMLResponse, include_in_schema=False)
async def ui_history(request: Request) -> HTMLResponse:
    """Render the run history page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the history page.
    """
    return _render(request, "history.html", {})


@router.get("/ui/errors", response_class=HTMLResponse, include_in_schema=False)
async def ui_errors(request: Request) -> HTMLResponse:
    """Render the errors page (failed runs).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the errors page.
    """
    return _render(request, "errors.html", {})


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


@router.get("/ui/fragments/history", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_history(
    request: Request, collection: str | None = None, run_status: str | None = None
) -> HTMLResponse:
    """Render the run history rows fragment for HTMX polling.

    Args:
        request: Incoming request.
        collection: Optional collection filter.
        run_status: Optional status filter.

    Returns:
        HTMLResponse with the history table body.
    """
    runs = await _runs_rows(collection=collection, run_status=run_status, limit=50)
    return _render(
        request,
        "partials/_runs.html",
        {"runs": runs, "status_labels": STATUS_LABELS},
    )


@router.get("/ui/fragments/errors", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_errors(request: Request) -> HTMLResponse:
    """Render the failed-runs fragment for HTMX polling.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the failed-runs table body.
    """
    runs = await _runs_rows(run_status="failed", limit=50)
    return _render(
        request,
        "partials/_runs.html",
        {"runs": runs, "status_labels": STATUS_LABELS},
    )


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
    if repo.get(name) is None:
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

    Args:
        name: Collection name.

    Returns:
        Redirect to history with a flash.
    """
    repo = CollectionsRepository()
    collection = repo.get(name)
    if collection is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    try:
        workflow_id = await TemporalAdminService().trigger_collection(collection)
    except Exception as exc:
        logger.error("ui_run_failed", collection=name, error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al lanzar: {exc}", "error")
    return _flash("/ui/history", f"Lanzada: {workflow_id}")


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
    return _render(request, "login.html", {})


@router.post("/ui/login", include_in_schema=False)
async def ui_login(request: Request) -> RedirectResponse:
    """Validate the admin token and set the session cookie.

    Args:
        request: Incoming request with the token form field.

    Returns:
        Redirect to /ui/collections on success, back to login on failure.
    """
    from src.api.auth import ADMIN_COOKIE_NAME
    from src.core.config import settings

    fields = await _form(request)
    token = fields.get("token", "").strip()
    expected = (settings.admin_api_token or "").strip()
    if not expected:
        return _flash(
            "/ui/login",
            "ADMIN_API_TOKEN no configurado. Añádelo al .env y reinicia.",
            "error",
        )
    if token != expected:
        return _flash("/ui/login", "Token incorrecto", "error")
    response = RedirectResponse("/ui/collections", status_code=302)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
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
