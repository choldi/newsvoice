"""Integration tests for the /ui/settings .env editor."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.services import env_editor
from src.core.config import settings

TOKEN = "secret-admin-token"
SECRET_VALUE = "secreto-que-no-puede-filtrarse"


@pytest.fixture(autouse=True)
def _env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the editor at an isolated .env and enable the admin auth."""
    path = tmp_path / ".env"
    path.write_text(
        "# --- LLM ---\n"
        "LLM_MODEL=openai/local-model\n"
        "LLM_VERIFY_THRESHOLD=0.9\n"
        "TTS_MAX_CHARS=5000\n"
        f"DOC_TTS_LINK_SECRET={SECRET_VALUE}\n"
        "TELEGRAM_CHAT_ID=42  # mi chat\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_editor, "ENV_FILE", path)
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    return path


def _client() -> TestClient:
    """Build a client authenticated through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def test_settings_requires_login() -> None:
    """Without a cookie the editor redirects to the login."""
    client = TestClient(app)
    response = client.get("/ui/settings", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/login"


def test_get_renders_groups_without_leaking_secrets() -> None:
    """The page lists sections and variables but never a stored secret."""
    client = _client()
    response = client.get("/ui/settings")

    assert response.status_code == 200
    body = response.text
    assert SECRET_VALUE not in body, "un secreto jamás debe llegar al HTML"
    assert 'name="env_LLM_VERIFY_THRESHOLD"' in body
    assert 'name="env_DOC_TTS_LINK_SECRET"' in body
    assert "deja vacío para conservar" in body
    assert "Verificación LLM (7.13)" in body
    assert "docker compose restart" in body


def test_post_valid_change_updates_only_that_line(_env_file: Path) -> None:
    """A valid POST rewrites the changed key and keeps everything else."""
    original = _env_file.read_text(encoding="utf-8")
    client = _client()

    response = client.post(
        "/ui/settings",
        data={
            "env_LLM_VERIFY_THRESHOLD": "0.75",
            "env_TTS_MAX_CHARS": "6000",
            "env_DOC_TTS_LINK_SECRET": "",  # vacío = conservar
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "Guardado" in body
    assert "docker compose restart" in body

    updated = _env_file.read_text(encoding="utf-8")
    assert "LLM_VERIFY_THRESHOLD=0.75" in updated
    assert "TTS_MAX_CHARS=6000" in updated
    assert f"DOC_TTS_LINK_SECRET={SECRET_VALUE}" in updated, "el secreto no se toca"
    assert "TELEGRAM_CHAT_ID=42  # mi chat" in updated, "los comentarios sobreviven"
    assert "# --- LLM ---" in updated
    # El contenido anterior queda en .env.bak
    assert (_env_file.parent / ".env.bak").read_text(encoding="utf-8") == original


def test_post_invalid_change_writes_nothing(_env_file: Path) -> None:
    """One bad value blocks the whole save and is reported per field."""
    original = _env_file.read_text(encoding="utf-8")
    client = _client()

    response = client.post(
        "/ui/settings",
        data={"env_LLM_VERIFY_THRESHOLD": "no-numero", "env_TTS_MAX_CHARS": "6000"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.text
    assert "No se ha guardado nada" in body
    assert "LLM_VERIFY_THRESHOLD" in body
    assert 'value="no-numero"' in body, "el valor tecleado se devuelve para corregirlo"
    assert _env_file.read_text(encoding="utf-8") == original, "el fichero no cambia"


def test_post_without_changes_reports_it(_env_file: Path) -> None:
    """Submitting the form untouched saves nothing and says so."""
    original = _env_file.read_text(encoding="utf-8")
    client = _client()

    response = client.post(
        "/ui/settings",
        data={
            "env_LLM_VERIFY_THRESHOLD": "0.9",
            "env_TTS_MAX_CHARS": "5000",
            "env_DOC_TTS_LINK_SECRET": "",
        },
        follow_redirects=False,
    )

    assert "Sin cambios" in response.text
    assert _env_file.read_text(encoding="utf-8") == original
    assert not (_env_file.parent / ".env.bak").exists()


def test_post_a_typed_secret_is_stored(_env_file: Path) -> None:
    """A non-empty secret input replaces the stored one."""
    client = _client()
    response = client.post(
        "/ui/settings",
        data={"env_DOC_TTS_LINK_SECRET": "rotated-secret"},
        follow_redirects=False,
    )

    assert "Guardado" in response.text
    assert "DOC_TTS_LINK_SECRET=rotated-secret" in _env_file.read_text(encoding="utf-8")
    assert "rotated-secret" not in response.text, "el valor tecleado no se refleja"
