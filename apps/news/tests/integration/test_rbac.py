"""Integration tests for Phase 6 RBAC: registration, OTP login and permissions.

Walks the full lifecycle a self-registered user goes through (register →
admin approval → emailed one-time code → session) and pins the role
matrix the middleware enforces on the GUI and the JSON API: viewer is
read-only, user gets the tools, admin keeps schedules/settings/users.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src.api.auth import ADMIN_COOKIE_NAME, create_session
from src.api.authz import Role
from src.api.db.database import LoginCodesRepository, UsersRepository
from src.api.main import app
from src.api.services.collections_repo import CollectionsRepository
from src.api.services.email import EmailService
from src.core.config import settings

#: One collection so run/narrate buttons have something to render against.
_COLLECTIONS_TOML = """[[collections]]
name = "tech"
time = "08:00"
feed_urls = ["https://example.com/rss"]
"""


@pytest.fixture(autouse=True)
def _rbac_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate collections and make auth depend on the seeded user rows.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Path to the isolated collections.toml file.
    """
    path = tmp_path / "collections.toml"
    path.write_text(_COLLECTIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(settings, "collections_file", str(path))
    monkeypatch.setattr(settings, "admin_api_token", "")
    monkeypatch.setattr(settings, "admin_username", "")
    return path


def _client() -> TestClient:
    """Build a fresh client with an empty cookie jar."""
    return TestClient(app)


async def _seed_user(email: str, role: str = "viewer", status: str = "active") -> dict:
    """Insert a user row in the requested state.

    Args:
        email: Email of the account.
        role: Role to assign ('user' or 'viewer').
        status: Initial status; non-pending rows are updated.

    Returns:
        The stored user row as a dict.
    """
    user = await UsersRepository().create(email, role=role)
    if status != "pending":
        await UsersRepository().set_status(int(user["id"]), status, role)
    return user


async def _cookie_for(identity: str, role: Role, user_id: int | None = None) -> dict[str, str]:
    """Start a session and return it as a cookie dict for TestClient.

    Args:
        identity: Display identity stored on the session.
        role: Role granted by the session.
        user_id: users.id for non-admin sessions.

    Returns:
        Mapping of the session cookie name to its raw token.
    """
    token = await create_session(identity, role, user_id=user_id)
    return {ADMIN_COOKIE_NAME: token}


def _capture_codes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the SMTP sender with a recorder for the plaintext codes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        List that collects ``(email, code)`` tuples as codes are sent.
    """
    sent: list[tuple[str, str]] = []

    async def fake_send(self: EmailService, email: str, code: str) -> None:
        sent.append((email, code))

    monkeypatch.setattr(EmailService, "send_login_code_email", fake_send)
    return sent


class TestRegistration:
    """POST /ui/register: accounts start pending until approved."""

    async def test_register_creates_pending_account(self) -> None:
        """A successful registration lands as pending with the viewer role."""
        client = _client()
        response = client.post(
            "/ui/register", data={"email": "new@example.com"}, follow_redirects=False
        )

        assert response.status_code == 302
        assert response.headers["location"].startswith("/ui/login")
        user = await UsersRepository().get_by_email("new@example.com")
        assert user is not None
        assert user["status"] == "pending"
        assert user["role"] == "viewer"

    async def test_register_rejects_invalid_email(self) -> None:
        """Malformed emails flash an error and store nothing."""
        client = _client()
        response = client.post(
            "/ui/register", data={"email": "not-an-email"}, follow_redirects=False
        )

        assert response.status_code == 302
        assert "level=error" in response.headers["location"]
        assert await UsersRepository().get_by_email("not-an-email") is None

    async def test_register_rejects_duplicate_email(self) -> None:
        """A second registration of the same email fails without new rows."""
        client = _client()
        client.post("/ui/register", data={"email": "dup@example.com"})
        response = client.post(
            "/ui/register", data={"email": "dup@example.com"}, follow_redirects=False
        )

        assert response.status_code == 302
        assert "level=error" in unquote(response.headers["location"])
        assert await UsersRepository().count() == 1


class TestAdminApproval:
    """/ui/users: only the admin approves and assigns roles."""

    async def test_admin_approves_and_assigns_role(self) -> None:
        """Approve flips pending → active with the role picked in the form."""
        client = _client()
        client.post("/ui/register", data={"email": "kid@example.com"})
        user = await UsersRepository().get_by_email("kid@example.com")
        assert user is not None
        admin = await _cookie_for("admin", Role.ADMIN)
        client.cookies.update(admin)

        response = client.post(
            f"/ui/users/{int(user['id'])}/approve",
            data={"role": "user"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "aprobado" in unquote(response.headers["location"])
        stored = await UsersRepository().get_by_email("kid@example.com")
        assert stored is not None
        assert stored["status"] == "active"
        assert stored["role"] == "user"
        assert stored["approved_at"] is not None

    async def test_admin_users_page_lists_accounts(self) -> None:
        """The admin sees every registered email on /ui/users."""
        await _seed_user("listed@example.com")
        client = _client()
        admin = await _cookie_for("admin", Role.ADMIN)
        client.cookies.update(admin)

        page = client.get("/ui/users", follow_redirects=False)

        assert page.status_code == 200
        assert "listed@example.com" in page.text
        assert "Aprobar" in page.text or "Guardar rol" in page.text

    async def test_viewer_cannot_open_users_page(self) -> None:
        """GET /ui/users as viewer redirects with a permission flash."""
        viewer = await _seed_user("v@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("v@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        response = client.get("/ui/users", follow_redirects=False)

        assert response.status_code == 302
        assert "permisos" in unquote(response.headers["location"])

    async def test_non_admin_cannot_approve(self) -> None:
        """A user-role session cannot approve; the account stays pending."""
        client = _client()
        client.post("/ui/register", data={"email": "held@example.com"})
        pending = await UsersRepository().get_by_email("held@example.com")
        assert pending is not None
        user = await _seed_user("actor@example.com", role="user")
        cookie = await _cookie_for("actor@example.com", Role.USER, int(user["id"]))
        client.cookies.update(cookie)

        response = client.post(
            f"/ui/users/{int(pending['id'])}/approve",
            data={"role": "user"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "permisos" in unquote(response.headers["location"])
        stored = await UsersRepository().get_by_email("held@example.com")
        assert stored is not None and stored["status"] == "pending"


class TestOneTimeLogin:
    """/ui/login/email + /ui/login/verify: the OTP flow end to end."""

    async def test_full_flow_logs_the_user_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Request → verify → session cookie, then the code is dead."""
        sent = _capture_codes(monkeypatch)
        await _seed_user("ana@example.com", role="viewer")
        client = _client()

        request_step = client.post(
            "/ui/login/email",
            data={"email": "ana@example.com"},
            follow_redirects=False,
        )

        assert request_step.status_code == 302
        assert "step=code" in request_step.headers["location"]
        assert "level=ok" in request_step.headers["location"]
        assert len(sent) == 1

        verify = client.post(
            "/ui/login/verify",
            data={"email": "ana@example.com", "code": sent[0][1]},
            follow_redirects=False,
        )

        assert verify.status_code == 302
        assert verify.headers["location"] == "/ui/collections"
        assert "httponly" in verify.headers.get("set-cookie", "").lower()

        page = client.get("/ui/collections", follow_redirects=False)
        assert page.status_code == 200
        assert "Nueva colección" not in page.text  # viewer is read-only

        replay = client.post(
            "/ui/login/verify",
            data={"email": "ana@example.com", "code": sent[0][1]},
            follow_redirects=False,
        )
        assert "level=error" in replay.headers["location"]
        assert "step=code" in replay.headers["location"]

    async def test_pending_account_cannot_request_a_code(self) -> None:
        """Approval is required before the login form will send anything."""
        await _seed_user("waiting@example.com", status="pending")
        client = _client()

        response = client.post(
            "/ui/login/email",
            data={"email": "waiting@example.com"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "level=error" in response.headers["location"]
        assert "step=code" not in response.headers["location"]

    async def test_unknown_email_cannot_request_a_code(self) -> None:
        """An unregistered address gets the registration hint as a flash."""
        client = _client()
        response = client.post(
            "/ui/login/email",
            data={"email": "ghost@example.com"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "level=error" in response.headers["location"]
        assert await LoginCodesRepository().latest("ghost@example.com") is None

    async def test_unconfigured_smtp_surfaces_a_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without SMTP the user is told the mail is not configured."""
        for field in ("smtp_host", "smtp_user", "smtp_password", "mail_from"):
            monkeypatch.setattr(settings, field, "")
        await _seed_user("noSMTP@example.com")
        client = _client()

        response = client.post(
            "/ui/login/email",
            data={"email": "noSMTP@example.com"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "level=error" in response.headers["location"]
        assert "SMTP" in unquote(response.headers["location"])
        assert await LoginCodesRepository().latest("noSMTP@example.com") is None

    async def test_resend_hits_the_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second request right away is bounced with a wait message."""
        sent = _capture_codes(monkeypatch)
        await _seed_user("spam@example.com")
        client = _client()
        client.post("/ui/login/email", data={"email": "spam@example.com"})

        response = client.post(
            "/ui/login/email",
            data={"email": "spam@example.com"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "level=error" in response.headers["location"]
        assert "step=code" not in response.headers["location"]
        assert len(sent) == 1

    async def test_wrong_code_flashes_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bad code keeps the user on the code step with an error flash."""
        sent = _capture_codes(monkeypatch)
        await _seed_user("brute@example.com")
        client = _client()
        client.post("/ui/login/email", data={"email": "brute@example.com"})
        wrong = "000000" if sent[0][1] != "000000" else "111111"

        response = client.post(
            "/ui/login/verify",
            data={"email": "brute@example.com", "code": wrong},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert "step=code" in response.headers["location"]
        assert "level=error" in response.headers["location"]

    async def test_logout_destroys_the_session(self) -> None:
        """After logout the same token no longer authenticates anything."""
        viewer = await _seed_user("bye@example.com", role="viewer")
        cookie = await _cookie_for("bye@example.com", Role.VIEWER, int(viewer["id"]))
        client = _client()
        client.cookies.update(cookie)

        response = client.post("/ui/logout", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/ui/login"
        # The delete_cookie response cleared the jar; replay the raw token.
        client.cookies.update(cookie)
        after = client.get("/ui/collections", follow_redirects=False)
        assert after.status_code == 302
        assert after.headers["location"] == "/ui/login"


class TestViewerPermissions:
    """viewer: every read, no write beyond logout."""

    async def test_reads_every_page(self) -> None:
        """All GUI pages render for a viewer session."""
        viewer = await _seed_user("reads@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("reads@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        for path in (
            "/ui/collections",
            "/ui/calendar",
            "/ui/runs",
            "/ui/tools",
            "/ui/voices",
        ):
            page = client.get(path, follow_redirects=False)
            assert page.status_code == 200, path

    async def test_action_buttons_are_hidden(self) -> None:
        """No admin or user affordances leak into a viewer's HTML."""
        viewer = await _seed_user("hidden@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("hidden@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        collections = client.get("/ui/collections", follow_redirects=False)
        assert collections.status_code == 200
        assert "Nueva colección" not in collections.text
        assert "Resincronizar" not in collections.text
        assert "Ejecutar ahora" not in collections.text
        assert "Generar podcast ahora" not in collections.text
        tools = client.get("/ui/tools", follow_redirects=False)
        assert tools.status_code == 200
        assert "/ui/tools/url" not in tools.text
        assert "/ui/tools/podcast" not in tools.text
        voices = client.get("/ui/voices", follow_redirects=False)
        assert voices.status_code == 200
        assert "/ui/voices/custom" not in voices.text
        assert "/ui/voices/clone" not in voices.text

    async def test_admin_pages_are_forbidden(self) -> None:
        """Settings and users redirect away with a permission flash."""
        viewer = await _seed_user("nofunc@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("nofunc@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        for path in ("/ui/settings", "/ui/users"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 302, path
            assert "permisos" in unquote(response.headers["location"]), path

    async def test_write_attempt_is_rejected_and_nothing_changes(self) -> None:
        """A posted delete bounces back and the collection survives."""
        viewer = await _seed_user("trywrite@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("trywrite@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        response = client.post("/ui/collections/tech/delete", follow_redirects=False)

        assert response.status_code == 302
        assert "permisos" in unquote(response.headers["location"])
        assert CollectionsRepository().get("tech") is not None

    async def test_api_write_returns_401_for_anonymous(self) -> None:
        """The JSON API answers 401 (not a redirect) with no credentials."""
        await _seed_user("someone@example.com")  # keeps auth enabled
        client = _client()

        response = client.post("/api/summarize", json={"url": "https://x.example"})

        assert response.status_code == 401
        body = response.json()
        assert body["status"] == "error"
        assert body["detail"] == "Authentication required"


class TestUserPermissions:
    """user: tools and run control, never schedules/settings/users."""

    async def test_tools_pages_render_with_forms(self) -> None:
        """The four tool forms are present for a user session."""
        user = await _seed_user("tooler@example.com", role="user")
        client = _client()
        cookie = await _cookie_for("tooler@example.com", Role.USER, int(user["id"]))
        client.cookies.update(cookie)

        tools = client.get("/ui/tools", follow_redirects=False)
        assert tools.status_code == 200
        assert "/ui/tools/url" in tools.text
        assert "/ui/tools/doc-tts" in tools.text
        assert "/ui/tools/lessons" in tools.text
        assert "/ui/tools/podcast" in tools.text

    async def test_collections_show_run_but_not_crud(self) -> None:
        """Run-now and narrate are in, create/edit/resync/delete are out."""
        user = await _seed_user("runner@example.com", role="user")
        client = _client()
        cookie = await _cookie_for("runner@example.com", Role.USER, int(user["id"]))
        client.cookies.update(cookie)

        page = client.get("/ui/collections", follow_redirects=False)
        assert page.status_code == 200
        assert "Ejecutar ahora" in page.text
        assert "Generar podcast ahora" in page.text
        assert "Nueva colección" not in page.text
        assert "Resincronizar" not in page.text
        assert "Eliminar" not in page.text

        delete = client.post("/ui/collections/tech/delete", follow_redirects=False)
        assert delete.status_code == 302
        assert "permisos" in unquote(delete.headers["location"])
        assert CollectionsRepository().get("tech") is not None

    async def test_admin_pages_are_forbidden(self) -> None:
        """A user cannot reach settings, users or the OpenAPI docs."""
        user = await _seed_user("limited@example.com", role="user")
        client = _client()
        cookie = await _cookie_for("limited@example.com", Role.USER, int(user["id"]))
        client.cookies.update(cookie)

        for path in ("/ui/settings", "/ui/users", "/docs"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 302, path

    async def test_admin_only_api_returns_403_json(self) -> None:
        """POST /api/cluster as user is denied with a role-annotated body."""
        user = await _seed_user("apilimited@example.com", role="user")
        client = _client()
        cookie = await _cookie_for("apilimited@example.com", Role.USER, int(user["id"]))
        client.cookies.update(cookie)

        response = client.post("/api/cluster", json={})

        assert response.status_code == 403
        body = response.json()
        assert body["status"] == "error"
        assert "requires role 'admin'" in body["detail"]
        assert "authenticated as 'user'" in body["detail"]


class TestAdminPermissions:
    """admin: full access through a session cookie."""

    async def test_admin_opens_admin_pages_and_writes(self) -> None:
        """Settings/users render and a collection can be created."""
        await _seed_user("presence@example.com")  # keeps auth genuinely on
        client = _client()
        admin = await _cookie_for("admin", Role.ADMIN)
        client.cookies.update(admin)

        assert client.get("/ui/settings", follow_redirects=False).status_code == 200
        assert client.get("/ui/users", follow_redirects=False).status_code == 200
        assert client.get("/docs", follow_redirects=False).status_code == 200

        created = client.post(
            "/ui/collections",
            data={"name": "science", "feed_urls": "https://example.com/rss"},
            follow_redirects=False,
        )
        assert created.status_code == 302
        assert "creada" in unquote(created.headers["location"])
        assert CollectionsRepository().get("science") is not None

    async def test_viewer_api_write_returns_403_json(self) -> None:
        """POST /api/summarize as viewer names the missing role."""
        viewer = await _seed_user("apiviewer@example.com", role="viewer")
        client = _client()
        cookie = await _cookie_for("apiviewer@example.com", Role.VIEWER, int(viewer["id"]))
        client.cookies.update(cookie)

        response = client.post("/api/summarize", json={})

        assert response.status_code == 403
        body = response.json()
        assert body["status"] == "error"
        assert "requires role 'user'" in body["detail"]
        assert "authenticated as 'viewer'" in body["detail"]
