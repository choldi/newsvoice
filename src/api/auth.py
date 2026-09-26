"""Admin authentication helpers for the Phase 6 GUI.

The GUI is protected by either:
- Username/password (preferred): when ADMIN_USERNAME and ADMIN_PASSWORD_HASH
  are configured. Password is verified with bcrypt.
- Legacy token: ADMIN_API_TOKEN when no username is set.

A login POST sets an HttpOnly cookie; a middleware guards both the `/ui/*`
pages and the `/api/admin/*` REST endpoints, redirecting (HTML) or returning
401 (JSON).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import bcrypt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.core.config import settings

ADMIN_COOKIE_NAME = "news_engine_admin"


def _password_ok(provided: str) -> bool:
    """Verify a provided password against the configured bcrypt hash.

    Args:
        provided: Plain password from the login form.

    Returns:
        True when the password matches the configured ADMIN_PASSWORD_HASH.
    """
    expected_hash = (settings.admin_password_hash or "").strip()
    if not expected_hash:
        return False
    try:
        return bcrypt.checkpw(provided.encode("utf-8"), expected_hash.encode("utf-8"))
    except Exception:
        return False


def _token_ok(provided: str) -> bool:
    """Compare a provided token against the configured one (legacy).

    Args:
        provided: Token from the request cookie/header.

    Returns:
        True when the token matches the configured ADMIN_API_TOKEN.
    """
    expected = (settings.admin_api_token or "").strip()
    return bool(expected) and provided == expected


def _auth_enabled() -> bool:
    """Return True if any auth method is configured."""
    return bool(settings.admin_username) or bool(settings.admin_api_token)


async def admin_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[object]]
) -> object:
    """Guard /ui/* and /api/admin/* behind the admin auth.

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

    # Allow access if no auth is configured at all
    if not _auth_enabled():
        return await call_next(request)

    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    if protected_api:
        token = request.headers.get("X-Admin-Token", token)

    # Verify token: either session cookie (username/password login) or legacy token
    if _password_ok(token) or _token_ok(token):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "detail": "Admin authentication required"},
        )
    return RedirectResponse("/ui/login", status_code=302)
