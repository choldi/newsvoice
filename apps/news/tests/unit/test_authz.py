"""Unit tests for the Phase 6 role map (src/api/authz).

The map is the single source of truth every middleware decision goes
through, so the matrix here pins public routes, the user-level
exceptions, the admin defaults and the privilege ordering.
"""

from __future__ import annotations

import pytest

from src.api.authz import (
    ASSIGNABLE_ROLES,
    ROLE_RANK,
    Role,
    has_role,
    required_role,
)


class TestRequiredRole:
    """required_role(): public, explicit and fallback decisions."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/health"),
            ("GET", "/static/htmx.min.js"),
            ("GET", "/ui/login"),
            ("POST", "/ui/login"),
            ("POST", "/ui/login/email"),
            ("POST", "/ui/login/verify"),
            ("GET", "/ui/register"),
            ("POST", "/ui/register"),
            ("POST", "/ui/logout"),
            ("GET", "/api/audio/report.pdf"),
            ("POST", "/api/generate_tts"),
        ],
    )
    def test_public_routes_need_no_role(self, method: str, path: str) -> None:
        """Public routes resolve to None so the middleware passes them through."""
        assert required_role(method, path) is None

    @pytest.mark.parametrize(
        "path",
        [
            "/ui/tools/url",
            "/ui/tools/jobs/doc_tts/run-1/delete",
            "/ui/collections/tech/run",
            "/ui/collections/tech/narrate",
            "/ui/runs/wf-1/terminate",
            "/api/summarize",
            "/api/summarize/url",
            "/api/mvp_digest",
            "/api/admin/lessons",
            "/api/admin/narration",
            "/api/admin/documents/tts",
            "/api/admin/audiobooks/submit",
            "/api/admin/collections/tech/run",
        ],
    )
    def test_tool_and_run_routes_require_user(self, path: str) -> None:
        """Tools, run-now and terminate are the user role's exceptions."""
        assert required_role("POST", path) is Role.USER

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/ui/settings"),
            ("POST", "/ui/settings"),
            ("GET", "/ui/phonetics"),
            ("POST", "/ui/phonetics"),
            ("GET", "/ui/phonetics/3/edit"),
            ("POST", "/ui/phonetics/3/update"),
            ("POST", "/ui/phonetics/3/delete"),
            ("GET", "/ui/users"),
            ("POST", "/ui/users/3/approve"),
            ("POST", "/ui/users/3/delete"),
            ("GET", "/docs"),
            ("GET", "/redoc"),
            ("GET", "/openapi.json"),
            ("POST", "/api/cluster"),
            ("POST", "/api/filter_new"),
        ],
    )
    def test_admin_routes_require_admin(self, method: str, path: str) -> None:
        """Settings, users, docs and cluster endpoints are admin only."""
        assert required_role(method, path) is Role.ADMIN

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/ui/collections"),
            ("GET", "/ui/tools"),
            ("GET", "/ui/runs"),
            ("GET", "/api/articles"),
        ],
    )
    def test_reads_default_to_viewer(self, method: str, path: str) -> None:
        """GET on a /ui or /api path needs only the read-only role."""
        assert required_role(method, path) is Role.VIEWER

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/ui/collections"),
            ("POST", "/ui/collections/tech/delete"),
            ("POST", "/ui/voices/custom"),
            ("POST", "/api/summarize/digest"),
            ("DELETE", "/api/articles/1"),
        ],
    )
    def test_writes_default_to_admin(self, method: str, path: str) -> None:
        """A write with no explicit rule stays admin: new endpoints are safe."""
        assert required_role(method, path) is Role.ADMIN

    def test_unknown_non_gui_path_defaults_to_admin(self) -> None:
        """Paths outside /ui and /api are admin-only by default."""
        assert required_role("GET", "/anything-else") is Role.ADMIN


class TestHasRole:
    """has_role(): privilege ordering and type tolerance."""

    @pytest.mark.parametrize(
        ("actual", "required", "expected"),
        [
            (Role.ADMIN, Role.ADMIN, True),
            (Role.ADMIN, Role.USER, True),
            (Role.ADMIN, Role.VIEWER, True),
            (Role.USER, Role.ADMIN, False),
            (Role.USER, Role.USER, True),
            (Role.USER, Role.VIEWER, True),
            (Role.VIEWER, Role.ADMIN, False),
            (Role.VIEWER, Role.USER, False),
            (Role.VIEWER, Role.VIEWER, True),
        ],
    )
    def test_role_matrix(self, actual: Role, required: Role, expected: bool) -> None:
        """admin ⊇ user ⊇ viewer, and a role always covers itself."""
        assert has_role(actual, required) is expected

    def test_none_required_is_always_allowed(self) -> None:
        """A public route (required None) admits anyone, even no identity."""
        assert has_role(None, None) is True

    def test_no_identity_never_satisfies_a_role(self) -> None:
        """An unauthenticated request (actual None) never passes a guard."""
        assert has_role(None, Role.VIEWER) is False

    @pytest.mark.parametrize(
        ("actual", "required", "expected"),
        [
            ("admin", "viewer", True),
            ("user", "admin", False),
            ("viewer", Role.USER, False),
            (Role.ADMIN, "user", True),
        ],
    )
    def test_accepts_strings_and_enums(
        self, actual: object, required: object, expected: bool
    ) -> None:
        """Callers pass both plain strings and Role members interchangeably."""
        assert has_role(actual, required) is expected  # type: ignore[arg-type]

    def test_role_rank_is_ordered(self) -> None:
        """The rank table must match the documented viewer < user < admin."""
        assert ROLE_RANK[Role.VIEWER] < ROLE_RANK[Role.USER] < ROLE_RANK[Role.ADMIN]


class TestAssignableRoles:
    """ASSIGNABLE_ROLES: what the admin can hand out."""

    def test_only_user_and_viewer_are_assignable(self) -> None:
        """The admin credential is env-only and can never be assigned."""
        assert ASSIGNABLE_ROLES == (Role.USER, Role.VIEWER)
        assert "admin" not in ASSIGNABLE_ROLES

    @pytest.mark.parametrize("role", ["user", "viewer"])
    def test_assignable_values_satisfy_admin_guard_when_typed(self, role: str) -> None:
        """Selectable values are exactly the strings the GUI posts."""
        assert role in ASSIGNABLE_ROLES
