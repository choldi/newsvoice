"""Integration tests for the Phase 7.16 phonetic heuristics admin GUI.

Walks the whole ``/ui/phonetics`` flow an operator uses to teach the
respeller a new language pair: the seeded table, the create/edit/delete
round trip, form validation and the admin-only role gate.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src.api.auth import ADMIN_COOKIE_NAME, create_session
from src.api.authz import Role
from src.api.db.database import UsersRepository
from src.api.main import app
from src.api.services import phonetic_heuristics
from src.core.config import settings

TOKEN = "secret-admin-token"


@pytest.fixture(autouse=True)
def _phonetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make login depend on the legacy admin token for every test."""
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "admin_username", "")
    return tmp_path


def _client() -> TestClient:
    """Build a client and authenticate it as the local admin."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


async def _seed_viewer(email: str) -> dict:
    """Insert an active viewer account for the role gate test.

    Args:
        email: Email of the account.

    Returns:
        The stored user row.
    """
    user = await UsersRepository().create(email, role="viewer")
    await UsersRepository().set_status(int(user["id"]), "active", "viewer")
    return user


def _flash_of(response: object) -> str:
    """Decode the flash message a redirect carries."""
    return unquote(response.headers["location"])  # type: ignore[attr-defined]


def test_phonetics_page_requires_login() -> None:
    """Anonymous visitors are sent to the login page."""
    client = TestClient(app)

    response = client.get("/ui/phonetics", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/ui/login"


async def test_viewer_cannot_open_the_page() -> None:
    """The heuristics gate the lesson pronunciation: admin only."""
    user = await _seed_viewer("v@example.com")
    token = await create_session("v@example.com", Role.VIEWER, int(user["id"]))
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE_NAME, token)

    response = client.get("/ui/phonetics", follow_redirects=False)

    assert response.status_code == 302
    assert "permisos" in _flash_of(response)


async def test_admin_sees_the_seeded_pairs_and_the_nav_link() -> None:
    """The table lists every stored pair and offers the create entry point."""
    client = _client()

    page = client.get("/ui/phonetics")
    rows = await phonetic_heuristics.list_all()

    assert page.status_code == 200
    assert "Adaptación fonética" in page.text
    assert page.text.count("Editar") == len(rows)
    assert 'href="/ui/phonetics/new"' in page.text
    assert '<a class="nav active" href="/ui/phonetics">Fonética</a>' in page.text
    # The migration seeds at least the narration pairs plus the lesson pair.
    assert len(rows) >= 3


async def test_create_edit_delete_roundtrip() -> None:
    """A new pair flows through create, edit, update and delete."""
    client = _client()

    created = client.post(
        "/ui/phonetics",
        data={
            "language_origin": "de",
            "language_target": "es",
            "language_origin_name": "German",
            "language_target_name": "Spanish",
            "suffixes": "chen\nung",
            "digraphs": "sch",
            "tech_terms": "Software",
            "native_words": "haus\nauto",
            "examples": "haus = os",
            "ascii_only": "on",
            "flag_all_foreign": "on",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    assert "guardado" in _flash_of(created)

    row = await phonetic_heuristics.get_pair("de", "es")
    assert row is not None
    assert row.suffixes == ["chen", "ung"]
    assert row.digraphs == ["sch"]
    assert row.native_words == ["haus", "auto"]
    assert row.examples == {"haus": "os"}
    assert row.flag_all_foreign is True and row.ascii_only is True

    edit = client.get(f"/ui/phonetics/{row.id}/edit")
    assert edit.status_code == 200
    assert 'value="de"' in edit.text
    assert "haus = os" in edit.text, "the stored map round-trips into the textarea"
    assert "chen" in edit.text

    updated = client.post(
        f"/ui/phonetics/{row.id}/update",
        data={
            "language_origin": "de",
            "language_target": "es",
            "suffixes": "chen",
            "native_words": "haus",
            "examples": "haus = os",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302
    assert "actualizado" in _flash_of(updated)

    refreshed = await phonetic_heuristics.get_pair("de", "es")
    assert refreshed is not None
    assert refreshed.id == row.id
    assert refreshed.suffixes == ["chen"]
    assert refreshed.native_words == ["haus"]
    assert refreshed.enabled is False, "an unticked box stores the row disabled"

    deleted = client.post(f"/ui/phonetics/{row.id}/delete", follow_redirects=False)
    assert deleted.status_code == 302
    assert "eliminada" in _flash_of(deleted)
    assert await phonetic_heuristics.get_pair("de", "es") is None

    again = client.post(f"/ui/phonetics/{row.id}/delete", follow_redirects=False)
    assert "not found" in _flash_of(again)


async def test_create_rejects_a_same_language_pair() -> None:
    """Form validation answers with a flash, never a 500."""
    client = _client()

    response = client.post(
        "/ui/phonetics",
        data={"language_origin": "es", "language_target": "es", "enabled": "on"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    flash = _flash_of(response)
    assert "Error al guardar" in flash
    assert "must differ" in flash
    assert await phonetic_heuristics.get_pair("es", "es") is None


async def test_create_rejects_a_non_iso_code() -> None:
    """A three-letter code never reaches the database."""
    client = _client()

    response = client.post(
        "/ui/phonetics",
        data={"language_origin": "esp", "language_target": "es", "enabled": "on"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "ISO 639-1" in _flash_of(response)
    assert await phonetic_heuristics.get_pair("esp", "es") is None


async def test_edit_reports_a_missing_row() -> None:
    """A stale id redirects with a message instead of a 404 page."""
    client = _client()

    response = client.get("/ui/phonetics/424242/edit", follow_redirects=False)

    assert response.status_code == 302
    assert "no encontrada" in _flash_of(response)
