"""Admin authentication helpers for the Phase 6 GUI.

The GUI is protected by a single shared token (`ADMIN_API_TOKEN`). A login POST
sets an HttpOnly cookie; a middleware guards both the `/ui/*` pages and the
`/api/admin/*` REST endpoints, redirecting (HTML) or returning 401 (JSON).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.core.config import settings

ADMIN_COOKIE_NAME = "news_engine_admin"


def _token_ok(provided: str) -> bool:
    """Compare a provided token against the configured one.

    Args:
        provided: Token from the request cookie/header.

    Returns:
        True when the token matches the configured ADMIN_API_TOKEN.
    """
    expected = (settings.admin_api_token or "").strip()
    return bool(expected) and provided == expected


async def admin_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[object]]
) -> object:
    """Guard /ui/* and /api/admin/* behind the admin token.

    Args:
        request: Incoming request.
        call_next: Next middleware/route handler.

    Returns:
        The response, or a redirect (HTML) / 401 (JSON) when unauthorized.
    """
    path = request.url.path
    protected_api = path.startswith("/api/admin/")
    protected_ui = path.startswith("/ui/") and path not in ("/ui/login", "/ui/logout")
    if not (protected_api or protected_ui):
        return await call_next(request)

    if protected_api:
        token = request.headers.get("X-Admin-Token", "")
    else:
        token = request.cookies.get(ADMIN_COOKIE_NAME, "")

    if _token_ok(token):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "detail": "Admin token required"},
        )
    return RedirectResponse("/ui/login", status_code=302)
