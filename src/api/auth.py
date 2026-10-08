"""Authentication and role-based authorization for the multi-user GUI (Phase 6).

Identity is resolved per request, in this order:

1. Session cookie (``news_engine_admin``) against the ``sessions`` table: a
   random 12 h token minted at login (replaces the legacy raw-password
   cookie).
2. ``X-Admin-Token`` header matching ``ADMIN_API_TOKEN`` (REST clients/tests).
3. Legacy fallback: a cookie/header equal to the raw admin password or
   ``ADMIN_API_TOKEN`` — nobody is logged out by the Phase 6 deploy.
4. No auth configured and no registered users -> open mode: every request
   is treated as ``admin`` (the pre-Phase 6 behaviour).

:func:`src.api.authz.required_role` decides whether a path is public. When
it is not, unauthenticated requests get 401 (JSON) or a redirect to the
login page (HTML); authenticated requests that lack the role get 403 (JSON)
or a redirect carrying a flash message. On success ``request.state.user``
and ``request.state.role`` are injected for route handlers and templates.
"""

from __future__ import annotations

import secrets
import urllib.parse
from collections.abc import Awaitable, Callable

import bcrypt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from src.api.authz import Role, has_role, required_role
from src.api.db.database import SessionsRepository, UsersRepository
from src.core.config import settings

ADMIN_COOKIE_NAME = "news_engine_admin"

#: Cookie/session lifetime; both expire together so the browser drops the
#: cookie exactly when the server-side row dies.
SESSION_TTL_HOURS = 12


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


async def _auth_enabled() -> bool:
    """Return True when the GUI must be guarded.

    Auth is on when an admin credential is configured (the normal case) or
    when at least one user has self-registered — otherwise a registration
    would never require a login. With neither, the engine stays open exactly
    like before Phase 6.

    Returns:
        True when at least one authentication path exists.
    """
    if settings.admin_username or settings.admin_api_token:
        return True
    return await UsersRepository().count() > 0


async def authenticate(request: Request) -> tuple[str, Role] | None:
    """Resolve the identity of a request, or None when unauthenticated.

    Args:
        request: Incoming request (cookies and headers are read).

    Returns:
        (identity, role) of the requester, or None when no valid credential
        was presented while auth is enabled.
    """
    if not await _auth_enabled():
        return ("admin", Role.ADMIN)

    cookie = request.cookies.get(ADMIN_COOKIE_NAME, "")
    if cookie:
        session = await SessionsRepository().get(cookie)
        if session is not None:
            return (str(session["identity"]), Role(session["role"]))
        # Legacy pre-Phase 6 cookie: the raw password or token itself.
        if _token_ok(cookie):
            return ("admin", Role.ADMIN)
        if _password_ok(cookie):
            return (settings.admin_username or "admin", Role.ADMIN)

    header = request.headers.get("X-Admin-Token", "")
    if header:
        if _token_ok(header):
            return ("admin", Role.ADMIN)
        if _password_ok(header):
            return (settings.admin_username or "admin", Role.ADMIN)

    return None


async def create_session(identity: str, role: Role, user_id: int | None = None) -> str:
    """Mint a session token and persist it.

    Args:
        identity: Display identity (email or admin username).
        role: Role snapshot stored with the session.
        user_id: users.id for non-admin sessions, None for the admin.

    Returns:
        The random token to store verbatim in the cookie.
    """
    token = secrets.token_urlsafe(32)
    await SessionsRepository().create(
        token,
        identity,
        role.value,
        user_id=user_id,
        ttl_hours=SESSION_TTL_HOURS,
    )
    return token


async def destroy_session(token: str) -> None:
    """Delete one session row (logout).

    Args:
        token: Raw token read from the cookie; no-op when empty or unknown.
    """
    if token:
        await SessionsRepository().delete(token)


def _flash_url(target: str, message: str, level: str = "error") -> str:
    """Attach a flash message to a redirect target.

    Args:
        target: Page URL to redirect to.
        message: Message to show on the target page.
        level: Flash level ('ok', 'warn' or 'error').

    Returns:
        The target URL carrying the flash in its query string.
    """
    sep = "&" if "?" in target else "?"
    return f"{target}{sep}flash={urllib.parse.quote(message)}&level={level}"


def _forbidden_target(role: Role, path: str) -> str:
    """Pick a safe page to flash 'no permissions' on.

    Redirects back to the page the action belongs to when the role may read
    that page (so the user sees the failed action in context), otherwise to
    the collections page — never to a path the role itself cannot GET, which
    would loop.

    Args:
        role: Role of the authenticated requester.
        path: Request path that was denied.

    Returns:
        A path the role is allowed to GET.
    """
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        root = "/" + "/".join(parts[:2])
        if root != "/ui" and has_role(role, required_role("GET", root)):
            return root
    return "/ui/collections"


async def admin_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[object]]
) -> object:
    """Guard every non-public route behind its required role.

    Consults :mod:`src.api.authz.required_role` for the incoming method and
    path. Public routes pass through untouched; authenticated requests get
    ``request.state.user``/``request.state.role`` injected before the route
    handler runs.

    Args:
        request: Incoming request.
        call_next: Next middleware/route handler.

    Returns:
        The response, or 401/redirect when unauthenticated, 403/redirect
        when the role is insufficient.
    """
    # CORS preflights carry no credentials; let CORSMiddleware answer them.
    if request.method == "OPTIONS":
        return await call_next(request)
    # HEAD reads like GET for authorization purposes.
    method = "GET" if request.method == "HEAD" else request.method
    path = request.url.path
    required = required_role(method, path)
    if required is None:
        return await call_next(request)

    identity_role = await authenticate(request)
    if identity_role is None:
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"status": "error", "detail": "Authentication required"},
            )
        return RedirectResponse("/ui/login", status_code=302)

    identity, role = identity_role
    if not has_role(role, required):
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=403,
                content={
                    "status": "error",
                    "detail": (
                        f"{method} {path} requires role '{required.value}' "
                        f"(authenticated as '{role.value}')"
                    ),
                },
            )
        target = _forbidden_target(role, path)
        return RedirectResponse(_flash_url(target, "No tienes permisos para esta acción"), 302)

    request.state.user = identity
    request.state.role = role
    return await call_next(request)
