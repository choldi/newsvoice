"""Role-based authorization map for the admin GUI and REST API (Phase 6).

Three roles, ordered ``admin > user > viewer``:

- ``admin``: everything (local ADMIN_USERNAME credential, never an OTP user).
- ``user``: read everything plus the tools (url/doc-tts/lessons/podcast),
  deleting generated audio, running/narrating a collection now and
  terminating workflows. Cannot create/edit collections (= schedules),
  resync schedules, touch voices or open Settings.
- ``viewer``: read-only — every page, table and audio download, no POST
  beyond logout.

``required_role`` is the single source of truth consulted by the middleware
in :mod:`src.api.auth`. Rules run top-down (most specific first) and an
unlisted path falls back to ``admin`` so a new endpoint is never exposed
by accident. ``None`` means the path is public.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Role(StrEnum):
    """Access roles, ordered from least to most privileged."""

    VIEWER = "viewer"
    USER = "user"
    ADMIN = "admin"


#: Privilege ranking; a role satisfies every requirement ranked below it.
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.USER: 1,
    Role.ADMIN: 2,
}

#: Roles a non-admin account can be assigned (``admin`` is env-only).
ASSIGNABLE_ROLES: tuple[str, ...] = (Role.USER, Role.VIEWER)

PUBLIC_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/static/")),
    ("*", re.compile(r"^/ui/login")),
    ("*", re.compile(r"^/ui/register")),
    ("*", re.compile(r"^/ui/logout")),
    # Signed audio links (HMAC bearer) and the staging webhook (own key).
    ("*", re.compile(r"^/api/audio/")),
    ("*", re.compile(r"^/api/internal/voices/staged/")),
    # Tombstone: always answers 410 Gone without side effects, so legacy
    # unauthenticated callers keep getting the "removed" signal, not a 401.
    ("POST", re.compile(r"^/api/generate_tts$")),
)

#: (method, path regex) -> minimum role, evaluated top-down before defaults.
_ROLE_RULES: tuple[tuple[str, re.Pattern[str], Role], ...] = (
    # --- user: tools, run-now and run control -----------------------------
    ("POST", re.compile(r"^/ui/tools/"), Role.USER),
    ("POST", re.compile(r"^/ui/collections/[^/]+/(run|narrate)$"), Role.USER),
    ("POST", re.compile(r"^/ui/runs/[^/]+/terminate$"), Role.USER),
    ("POST", re.compile(r"^/api/admin/documents/tts$"), Role.USER),
    ("POST", re.compile(r"^/api/admin/lessons$"), Role.USER),
    ("POST", re.compile(r"^/api/admin/narration$"), Role.USER),
    ("POST", re.compile(r"^/api/admin/collections/[^/]+/run$"), Role.USER),
    ("POST", re.compile(r"^/api/summarize/url$"), Role.USER),
    ("POST", re.compile(r"^/api/summarize$"), Role.USER),
    ("POST", re.compile(r"^/api/mvp_digest$"), Role.USER),
    # --- admin: everything else ------------------------------------------
    ("*", re.compile(r"^/ui/settings"), Role.ADMIN),
    ("*", re.compile(r"^/ui/users"), Role.ADMIN),
    ("*", re.compile(r"^/docs$|^/redoc$|^/openapi\.json$"), Role.ADMIN),
    ("*", re.compile(r"^/api/cluster$|^/api/filter_new$"), Role.ADMIN),
)


def required_role(method: str, path: str) -> Role | None:
    """Return the minimum role a request needs, or ``None`` when public.

    Args:
        method: HTTP method of the request.
        path: Request path (no query string).

    Returns:
        The required :class:`Role`, or ``None`` for a public route. Paths
        with no matching rule default to ``Role.ADMIN``.
    """
    for rule_method, pattern in PUBLIC_ROUTES:
        if rule_method in ("*", method.upper()) and pattern.match(path):
            return None
    for rule_method, pattern, role in _ROLE_RULES:
        if rule_method in ("*", method.upper()) and pattern.match(path):
            return role
    if path == "/ui" or path.startswith("/ui/") or path.startswith("/api/"):
        # GET reads are open to every authenticated role; writes need admin.
        if method.upper() == "GET":
            return Role.VIEWER
        return Role.ADMIN
    return Role.ADMIN


def has_role(actual: Role | str | None, required: Role | str | None) -> bool:
    """Check whether ``actual`` satisfies ``required``.

    Args:
        actual: Role of the authenticated requester (or None).
        required: Minimum role the endpoint demands (None = no requirement).

    Returns:
        True when the role rank of ``actual`` covers ``required``.
    """
    if required is None:
        return True
    if actual is None:
        return False
    return ROLE_RANK[Role(actual)] >= ROLE_RANK[Role(required)]
