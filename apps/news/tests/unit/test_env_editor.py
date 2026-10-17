"""Unit tests for the .env settings editor service."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.services import env_editor
from src.api.services.env_editor import (
    apply_changes,
    build_schema,
    compute_changes,
    form_context,
    parse_env,
    save_env,
    saved_flash,
    to_env_value,
    validate_changes,
)
from src.core.config import Settings


@pytest.fixture(autouse=True)
def _env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the editor at an isolated .env."""
    path = tmp_path / ".env"
    path.write_text(
        "# --- LLM ---\n"
        "LLM_MODEL=openai/local-model\n"
        "LLM_VERIFY_THRESHOLD=0.9\n"
        "TTS_MAX_CHARS=5000\n"
        "DOC_TTS_LINK_SECRET=secreto-actual\n"
        "TELEGRAM_CHAT_ID=42  # mi chat\n"
        "# final del fichero\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_editor, "ENV_FILE", path)
    return path


BASE = {
    "LLM_MODEL": "openai/local-model",
    "LLM_VERIFY_THRESHOLD": "0.9",
    "TTS_MAX_CHARS": "5000",
    "DOC_TTS_LINK_SECRET": "secreto-actual",
    "TELEGRAM_CHAT_ID": "42",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_covers_every_settings_field() -> None:
    """The schema mirrors Settings.model_fields one for one."""
    schema = build_schema()
    assert len(schema) == len(Settings.model_fields)
    by_name = {spec.name: spec for spec in schema}
    assert by_name["llm_model"].env_name == "LLM_MODEL"
    assert by_name["doc_tts_link_secret"].env_name == "DOC_TTS_LINK_SECRET"


def test_schema_groups_sections_by_longest_prefix() -> None:
    """llm_verify_* wins over llm_*; each phase lands in its section."""
    sections = {spec.env_name: spec.section for spec in build_schema()}
    assert sections["LLM_VERIFY_THRESHOLD"] == "Verificación LLM (7.13)"
    assert sections["LLM_MODEL"] == "LLM del digest"
    assert sections["DOC_TTS_LINK_SECRET"] == "TTS de documentos (7.11)"
    assert sections["TELEGRAM_BOT_TOKEN"] == "Telegram"
    assert sections["ADMIN_API_TOKEN"] == "Admin / GUI"
    assert sections["DATABASE_PATH"] == "API"


def test_schema_flags_secrets_but_not_counters() -> None:
    """Real secrets are flagged; MAX_TOKENS-style counters are not."""
    secrets = {spec.env_name for spec in build_schema() if spec.is_secret}
    assert {"TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "DOC_TTS_LINK_SECRET"} <= secrets
    assert "LLM_MAX_TOKENS" not in secrets
    assert "QWEN_TTS_MAX_NEW_TOKENS" not in secrets
    assert "LLM_API_BASE" not in secrets


def test_schema_hint_shows_ranges() -> None:
    """A bounded field advertises its range so the form can warn before POST."""
    by_name = {spec.name: spec for spec in build_schema()}
    assert "0.0 – 1.0" in by_name["llm_verify_threshold"].hint


# ---------------------------------------------------------------------------
# Value serialization and .env parsing
# ---------------------------------------------------------------------------


def test_to_env_value_serializes_types() -> None:
    """Booleans, lists and numbers use the forms pydantic-settings parses."""
    assert to_env_value(True) == "true"
    assert to_env_value(False) == "false"
    assert to_env_value(["a", "b"]) == '["a", "b"]'
    assert to_env_value(0.5) == "0.5"
    assert to_env_value(None) == ""
    assert to_env_value("texto") == "texto"


def test_parse_env_keeps_lines_quotes_and_inline_comments() -> None:
    """Parsing indexes assignments without losing a single original line."""
    text = (
        "# cabecera\n"
        "\n"
        "LLM_MODEL=openai/local-model\n"
        'ADMIN_USERNAME="con espacios"\n'
        "TELEGRAM_CHAT_ID=42  # mi chat\n"
        "OTRO='# con numeral'\n"
    )
    document = parse_env(text)

    assert document.lines == text.splitlines()
    assert document.entries["LLM_MODEL"].value == "openai/local-model"
    assert document.entries["ADMIN_USERNAME"].value == "con espacios"
    assert document.entries["TELEGRAM_CHAT_ID"].value == "42"
    assert document.entries["TELEGRAM_CHAT_ID"].inline_comment == "# mi chat"
    assert document.entries["OTRO"].value == "# con numeral"


def test_parse_env_ignores_malformed_lines() -> None:
    """Lines without a valid KEY= are comments/garbage, not assignments."""
    document = parse_env("no-igual\n1MALFORMED=x\n# KEY= comentario\n")
    assert document.entries == {}


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------


def test_compute_changes_only_returns_edits() -> None:
    """Unchanged fields, blank secrets and unknown keys never count."""
    fields = build_schema()
    submitted = {
        "LLM_VERIFY_THRESHOLD": "0.9",  # igual que el actual
        "TTS_MAX_CHARS": "6000",  # cambiado
        "DOC_TTS_LINK_SECRET": "",  # secreto vacío = conservar
        "NO_EXISTE": "x",  # fuera del esquema
    }
    changes = compute_changes(fields, submitted, dict(BASE))
    assert changes == {"TTS_MAX_CHARS": "6000"}


def test_compute_changes_accepts_a_typed_secret() -> None:
    """A non-empty secret input is a change; the old one is replaced."""
    fields = build_schema()
    changes = compute_changes(fields, {"DOC_TTS_LINK_SECRET": "nuevo-secreto"}, dict(BASE))
    assert changes == {"DOC_TTS_LINK_SECRET": "nuevo-secreto"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_accepts_a_clean_batch() -> None:
    """Typed, in-range values produce no problems."""
    fields = build_schema()
    _, problems = validate_changes(
        fields,
        {"LLM_VERIFY_THRESHOLD": "0.75", "TTS_MAX_CHARS": "6000", "LLM_MODEL": "openai/x"},
    )
    assert problems == []


def test_validate_rejects_a_non_numeric_int() -> None:
    """An int field with text is reported against its env name."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"TTS_MAX_CHARS": "mucho"})
    assert len(problems) == 1
    assert problems[0].env_name == "TTS_MAX_CHARS"
    assert "tipo" in problems[0].message


def test_validate_rejects_an_out_of_range_number() -> None:
    """pydantic's ge/le are the single source of truth for ranges."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"LLM_VERIFY_THRESHOLD": "1.5"})
    assert len(problems) == 1
    assert problems[0].env_name == "LLM_VERIFY_THRESHOLD"


def test_validate_rejects_an_unknown_backend() -> None:
    """Bounded strings carry an explicit rule in EXTRA_RULES."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"LLM_VERIFY_BACKEND": "pepe"})
    assert len(problems) == 1
    assert "local" in problems[0].message


def test_validate_rejects_broken_json_for_lists() -> None:
    """Complex fields must be valid JSON, with a readable error."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"MONITOR_NOTIFY_CHANNELS": "telegram, email"})
    assert len(problems) == 1
    assert problems[0].env_name == "MONITOR_NOTIFY_CHANNELS"
    assert "JSON" in problems[0].message


def test_validate_accepts_json_for_lists() -> None:
    """A well-formed JSON list passes through the Settings model."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"MONITOR_NOTIFY_CHANNELS": '["telegram"]'})
    assert problems == []


def test_validate_keeps_a_blank_secret_untouched() -> None:
    """Blank secrets validate against the stored value, not against empty."""
    fields = build_schema()
    _, problems = validate_changes(fields, {"DOC_TTS_LINK_SECRET": ""})
    assert problems == []


# ---------------------------------------------------------------------------
# Rewrite
# ---------------------------------------------------------------------------


def test_apply_changes_updates_only_the_edited_lines() -> None:
    """Untouched lines stay byte-identical; inline comments survive."""
    document = parse_env(Path(env_editor.ENV_FILE).read_text(encoding="utf-8"))
    updated = apply_changes(document, {"LLM_VERIFY_THRESHOLD": "0.75", "TTS_MAX_CHARS": "6000"})

    assert "LLM_VERIFY_THRESHOLD=0.75" in updated
    assert "TTS_MAX_CHARS=6000" in updated
    assert "LLM_MODEL=openai/local-model" in updated
    assert "TELEGRAM_CHAT_ID=42  # mi chat" in updated
    assert "# --- LLM ---" in updated
    assert "secreto-actual" in updated  # sin cambios: no se toca


def test_apply_changes_appends_new_keys_under_one_marker() -> None:
    """Keys absent from the file are appended once, with a single marker."""
    document = parse_env(Path(env_editor.ENV_FILE).read_text(encoding="utf-8"))
    updated = apply_changes(document, {"NUEVA_CLAVE": "x", "OTRA_CLAVE": "y"})

    assert updated.count("# añadido por el editor de configuración") == 1
    assert updated.index("NUEVA_CLAVE=x") < updated.index("OTRA_CLAVE=y")
    assert updated.rstrip().endswith("OTRA_CLAVE=y")


def test_save_env_writes_and_keeps_a_backup(_env_file: Path) -> None:
    """Saving is atomic and the previous content lands in .env.bak."""
    original = _env_file.read_text(encoding="utf-8")
    save_env(_env_file, {"LLM_VERIFY_THRESHOLD": "0.5"})

    assert "LLM_VERIFY_THRESHOLD=0.5" in _env_file.read_text(encoding="utf-8")
    backup = _env_file.parent / ".env.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Form context (what the template sees)
# ---------------------------------------------------------------------------


def test_form_context_never_renders_a_secret() -> None:
    """Secret inputs are empty; the placeholder says if one is stored."""
    fields = build_schema()
    sections = form_context(fields)
    rows = {row["env_name"]: row for section in sections for row in section["fields"]}

    secret = rows["DOC_TTS_LINK_SECRET"]
    assert secret["value"] == ""
    assert secret["is_secret"] is True
    assert secret["configured"] is True
    assert "deja vacío para conservar" in secret["placeholder"]

    plain = rows["LLM_VERIFY_THRESHOLD"]
    assert plain["value"] == "0.9"
    assert plain["is_secret"] is False


def test_form_context_echoes_the_input_and_the_error_on_failure() -> None:
    """A rejected POST shows what was typed plus the message."""
    fields = build_schema()
    problems = [env_editor.ValidationProblem("LLM_VERIFY_THRESHOLD", "rango inválido")]
    sections = form_context(
        fields,
        submitted={"LLM_VERIFY_THRESHOLD": "9.9", "DOC_TTS_LINK_SECRET": "nuevo"},
        problems=problems,
    )
    rows = {row["env_name"]: row for section in sections for row in section["fields"]}

    assert rows["LLM_VERIFY_THRESHOLD"]["value"] == "9.9"
    assert rows["LLM_VERIFY_THRESHOLD"]["error"] == "rango inválido"
    # Even with a typed secret, nothing is echoed back.
    assert rows["DOC_TTS_LINK_SECRET"]["value"] == ""
    assert "se guardará" in rows["DOC_TTS_LINK_SECRET"]["placeholder"]


def test_saved_flash_carries_the_restart_command() -> None:
    """The success message must tell the operator how to apply the change."""
    flash = saved_flash({"LLM_MODEL": "x"})
    assert "Guardado (1 cambio(s))" in flash
    assert "docker compose restart" in flash
    assert "teacher-worker" in flash
