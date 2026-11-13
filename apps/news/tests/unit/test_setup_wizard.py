"""Tests for the guided setup wizard (``scripts/setup.py``).

The wizard lives outside the application package, so the suite adds
``scripts/`` to ``sys.path`` through ``pythonpath`` in ``pyproject.toml``.
Everything here is hermetic: no repository file is ever written and no host
command with side effects is executed.
"""

from __future__ import annotations

import curses
import shutil
from pathlib import Path

import pytest
from setup_wizard import deps
from setup_wizard import validate as validation
from setup_wizard.app import (
    Context,
    _confirm_write,
    _confirm_write_one,
    _shared_key_conflicts,
    _unify_shared_key,
    _write_env,
    apply_screen,
    build_app,
    env_screen,
    final_screen,
    load_context,
    method_screen,
    prereq_screen,
    profile_screen,
    review_screen,
    validate_screen,
    welcome_screen,
)
from setup_wizard.curses_ui import App, Row, Screen
from setup_wizard.detect import Finding, Report, prerequisites
from setup_wizard.envfile import EnvFile
from setup_wizard.guide import GUIDE_PAGES
from setup_wizard.plain import run_plain
from setup_wizard.settings import PROFILES, fields_for, hash_password, sections_for

DEPLOY_EXAMPLE = (
    "# plantilla compose\n"
    "API_PORT=8000\n"
    "GATEWAY_PORT=8001\n"
    "GATEWAY_API_KEY=\n"
    "COMPOSE_PROFILES=news,gateway\n"
)
NEWS_EXAMPLE = (
    "# plantilla news\n"
    "LLM_API_BASE=http://host.docker.internal:8080/v1\n"
    "LLM_MODEL=openai/local-model\n"
    "API_PORT=8000\n"
    "DATABASE_PATH=data/news.db\n"
    "ADMIN_USERNAME=\n"
    "ADMIN_PASSWORD_HASH=\n"
    "ADMIN_API_TOKEN=\n"
    "# TTS_API_BASE_URL=http://gateway:8000/api/v1\n"
    "TTS_API_BASE_URL=http://gateway:8000/api/v1\n"
    "TTS_API_KEY=\n"
    "TTS_DEFAULT_BACKEND=edge\n"
    "TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here\n"
    "DOC_TTS_LINK_SECRET=\n"
)
GATEWAY_EXAMPLE = (
    "# plantilla gateway\n"
    "TTS_ENGINE=routed\n"
    'TTS_ENGINES={"es":"pocket_tts"}\n'
    "API_KEY=\n"
    "TTS_DEVICE=cuda:0\n"
)


def _write_env_files(root: Path) -> dict[str, EnvFile]:
    """Create a miniature repository with the three env templates.

    Args:
        root: Temporary directory used as the repository root.

    Returns:
        The loaded :class:`EnvFile` objects keyed by file id.
    """
    specs = {
        "deploy": (root / "deploy" / ".env", DEPLOY_EXAMPLE),
        "news": (root / "apps" / "news" / ".env", NEWS_EXAMPLE),
        "gateway": (root / "apps" / "gateway" / ".env", GATEWAY_EXAMPLE),
    }
    envs: dict[str, EnvFile] = {}
    for key, (path, text) in specs.items():
        example = path.with_name(".env.example")
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text(text, encoding="utf-8")
        envs[key] = EnvFile.load(path, example)
    return envs


def _context(envs: dict[str, EnvFile]) -> Context:
    """Build a context with an empty (but valid) detection report."""
    return Context(report=Report(python="3.12.0"), envs=envs)


# -- env file ---------------------------------------------------------------


def test_env_file_reads_template_and_unlocks_commented_keys(tmp_path: Path) -> None:
    """A ``# KEY=`` line becomes an active setting as soon as it is staged."""
    envs = _write_env_files(tmp_path)
    gateway = envs["gateway"]

    assert not gateway.exists
    assert gateway.from_example
    assert gateway.get("TTS_ENGINE") == "routed"

    gateway.set("TTS_ENGINE", "mock")
    rendered = gateway.render()
    assert "\nTTS_ENGINE=mock\n" in rendered
    assert "API_KEY=" in rendered

    assert gateway.diff_against_disk() == [("TTS_ENGINE", "routed", "mock")]
    assert gateway.changed_keys() == ["TTS_ENGINE"]


def test_env_file_keeps_pristine_values_and_backs_up(tmp_path: Path) -> None:
    """The original value stays available after the buffer is rewritten."""
    envs = _write_env_files(tmp_path)
    deploy = envs["deploy"]
    deploy.set("API_PORT", "9000")

    assert deploy.original("API_PORT") == "8000"
    assert deploy.diff_against_disk() == [("API_PORT", "8000", "9000")]

    assert deploy.write() is None  # target did not exist yet
    assert deploy.exists
    assert "API_PORT=9000" in deploy.path.read_text(encoding="utf-8")

    deploy.set("API_PORT", "7000")
    backup = deploy.write()
    assert backup is not None and backup.exists()
    assert "API_PORT=9000" in backup.read_text(encoding="utf-8")

    reloaded = EnvFile.load(deploy.path, deploy.example)
    assert reloaded.get("API_PORT") == "7000"
    assert reloaded.pristine["API_PORT"] == "7000"


# -- settings ---------------------------------------------------------------


def test_every_field_is_well_formed_and_unique() -> None:
    """Fields point at a real file and no label repeats inside one file."""
    allowed_files = {"deploy", "news", "gateway"}
    allowed_kinds = {"text", "secret", "choice", "bool", "password"}
    for file_key in allowed_files:
        labels: set[str] = set()
        for field in fields_for(file_key):
            assert field.file == file_key
            assert field.key and field.label and field.help
            assert field.kind in allowed_kinds
            assert field.label not in labels, f"etiqueta repetida: {field.label}"
            labels.add(field.label)
            if field.kind == "choice":
                assert field.options, f"{field.key} es una elección sin opciones"
            if field.generate:
                assert field.kind == "secret", f"{field.key} genera un secreto no secreto"


def test_sections_cover_exactly_the_declared_fields() -> None:
    """``sections_for`` and ``fields_for`` are two views of the same data."""
    for file_key in ("deploy", "news", "gateway"):
        from_sections = [f.key for section in sections_for(file_key) for f in section.fields]
        assert from_sections == [f.key for f in fields_for(file_key)]


def test_tts_fields_expose_cloud_engines_and_sidecars() -> None:
    """The TUI keeps the new engines and their optional credentials reachable."""
    gateway = {field.key: field for field in fields_for("gateway")}
    news = {field.key: field for field in fields_for("news")}

    assert {"qwen3_tts", "omnivoice_remote", "fish_tts", "openai_tts"} <= set(
        gateway["TTS_ENGINE"].options
    )
    assert {"kokoro", "fish_tts", "openai_tts"} <= set(news["TTS_DEFAULT_BACKEND"].options)
    assert gateway["FISH_API_KEY"].kind == "secret"
    assert gateway["OPENAI_TTS_BASE_URL"].advanced
    assert gateway["QWEN3_TTS_REMOTE_URL"].advanced
    assert gateway["QWEN3_TTS_REMOTE_TIMEOUT"].default == "300"


def test_profiles_only_touch_keys_that_exist() -> None:
    """Every profile default targets a declared field of that file."""
    known = {
        (file_key, key)
        for file_key in ("deploy", "news", "gateway")
        for key in map_field_keys(file_key)
    }
    for profile in PROFILES:
        assert profile.label and profile.description
        for target in profile.defaults:
            assert target in known, f"{profile.key} referencia {target} inexistente"


def map_field_keys(file_key: str) -> list[str]:
    """Return the variable names of one env file."""
    return [field.key for field in fields_for(file_key)]


def test_hash_password_round_trip() -> None:
    """The admin password dialog produces a bcrypt hash."""
    digest = hash_password("secreta-123")
    if digest is None:
        pytest.skip("bcrypt no instalado en este intérpreprete")
    assert digest.startswith("$2")

    import bcrypt

    assert bcrypt.checkpw(b"secreta-123", digest.encode("ascii"))


# -- validation -------------------------------------------------------------


def test_validate_reports_missing_required_and_port_mismatch(tmp_path: Path) -> None:
    """Missing required values and disagreeing ports fail hard."""
    envs = _write_env_files(tmp_path)
    envs["news"].set("LLM_API_BASE", "")
    envs["deploy"].set("API_PORT", "9000")

    checks = validation.validate(envs, Report(python="3.12.0"), will_write=True)
    by_name = {check.name: check for check in checks}

    assert by_name["news obligatorias"].status == "bad"
    assert by_name["puertos"].status == "bad"
    assert "9000" in by_name["puertos"].detail


def test_validate_flags_shared_key_mismatch_and_placeholders(tmp_path: Path) -> None:
    """A gateway key that disagrees across files is an error, not a warning."""
    envs = _write_env_files(tmp_path)
    envs["deploy"].set("GATEWAY_API_KEY", "aaaa")
    envs["news"].set("TTS_API_KEY", "bbbb")

    checks = validation.validate(envs, Report(python="3.12.0"), will_write=True)
    by_name = {check.name: check for check in checks}

    assert by_name["clave compartida"].status == "bad"
    assert "TTS_API_KEY" in by_name["clave compartida"].detail
    assert by_name["news plantillas"].status == "warn"  # placeholder del bot de Telegram
    assert by_name["acceso al panel"].status == "warn"  # sin credencial admin


def test_missing_llm_endpoint_severity_depends_on_the_profile(tmp_path: Path) -> None:
    """A container cannot reach ``localhost``, a host run can."""
    envs = _write_env_files(tmp_path)
    envs["news"].set("LLM_API_BASE", "")

    report = Report(python="3.12.0")
    docker = validation.validate(envs, report, will_write=True, profile="docker")
    local = validation.validate(envs, report, will_write=True, profile="local")

    assert {c.status for c in docker if c.name == "news obligatorias"} == {"bad"}
    assert {c.status for c in local if c.name == "news obligatorias"} == {"warn"}


def test_validate_accepts_a_consistent_configuration(tmp_path: Path) -> None:
    """A fully filled configuration has no blocking error."""
    envs = _write_env_files(tmp_path)
    envs["deploy"].set("GATEWAY_API_KEY", "a" * 64)
    envs["news"].set("TTS_API_KEY", "a" * 64)
    envs["news"].set("ADMIN_API_TOKEN", "b" * 64)
    envs["news"].set("DOC_TTS_LINK_SECRET", "c" * 64)
    envs["news"].set("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF")
    envs["gateway"].set("API_KEY", "a" * 64)

    checks = validation.validate(envs, Report(python="3.12.0"), will_write=True)
    by_name = {check.name: check for check in checks}

    assert not [check for check in checks if check.status == "bad"], checks
    assert by_name["clave compartida"].status == "ok"
    assert by_name["acceso al panel"].status == "ok"


def test_validate_missing_files_are_bad_only_when_they_will_not_be_written(
    tmp_path: Path,
) -> None:
    """During the wizard an unwritten file is a warning; in audit mode it fails."""
    envs = _write_env_files(tmp_path)

    audit = validation.validate(envs, Report(python="3.12.0"), will_write=False)
    wizard = validation.validate(envs, Report(python="3.12.0"), will_write=True)

    assert {c.status for c in audit if c.name.endswith("/.env")} == {"bad"}
    assert {c.status for c in wizard if c.name.endswith("/.env")} == {"warn"}


# -- dependency plan --------------------------------------------------------


def test_plan_covers_every_documented_method() -> None:
    """Each method yields a command list, and skipping yields nothing."""
    assert deps.plan("skip") == []

    uv_steps = deps.plan("uv")
    assert len(uv_steps) == 1
    assert uv_steps[0].command[:2] == ["uv", "sync"]

    venv_steps = deps.plan("venv")
    assert len(venv_steps) == 4
    assert all(".venv" in " ".join(map(str, step.command)) for step in venv_steps)
    assert venv_steps[1].cwd.name == "news"
    assert venv_steps[3].cwd.name == "gateway"


def test_method_available_reports_a_reason() -> None:
    """A missing tool is reported instead of failing later."""
    ok, reason = deps.method_available("skip")
    assert ok and reason == ""

    ok, reason = deps.method_available("uv")
    assert isinstance(ok, bool)
    if not ok:
        assert "uv" in reason


def test_execute_runs_steps_and_reports_progress(tmp_path: Path) -> None:
    """The runner feeds the callback and stops at a required failure."""
    events: list[tuple[str, str, str]] = []
    good = deps.Step(title="ok", command=["python3", "-c", "print('hola')"], cwd=tmp_path)
    bad = deps.Step(title="bad", command=["python3", "-c", "raise SystemExit(3)"], cwd=tmp_path)

    assert deps.execute([good, bad], lambda *args: events.append(args)) is False
    assert [status for status, _title, _detail in events] == ["run", "ok", "run", "bad"]
    assert events[1][1] == "ok" and events[3][1] == "bad"


# -- curses toolkit ---------------------------------------------------------


def _demo_app() -> App:
    """Return an app holding one screen with a text field and a button."""
    app = App("test")
    app.push(
        Screen(
            title="Demo",
            rows=[
                Row("heading", label="Sección"),
                Row("text", label="Nombre", value="", help="ayuda"),
                Row("secret", label="Clave", value="abc123", generate="hex32", secret=True),
                Row("button", label="Continuar", action="next"),
            ],
            footer="pies",
        )
    )
    return app


def test_navigation_moves_between_focusable_rows() -> None:
    """Arrow keys skip headings and wrap the focus on real fields."""
    app = _demo_app()
    rows = app.current.rows
    assert app.focus == 1  # heading is not focusable

    app.handle(curses.KEY_UP)
    assert rows[app.focus].label == "Continuar"  # wrapped from the first row
    app.handle(curses.KEY_DOWN)
    assert rows[app.focus].label == "Nombre"
    app.handle(curses.KEY_DOWN)
    assert rows[app.focus].label == "Clave"
    app.handle(curses.KEY_DOWN)
    assert rows[app.focus].label == "Continuar"
    app.handle(curses.KEY_DOWN)
    assert rows[app.focus].label == "Nombre"  # wrapped back around


def test_text_editing_commits_and_cancels() -> None:
    """Enter starts editing, characters append, Enter commits."""
    app = _demo_app()
    row = app.current.rows[1]

    app.handle(10)  # Enter → edit
    assert app.editing is row
    app.handle(ord("A"))
    app.handle(ord("b"))
    assert row.value == "Ab"
    app.handle(curses.KEY_BACKSPACE)
    assert row.value == "A"
    app.handle(10)  # commit
    assert app.editing is None and row.value == "A"

    app.handle(10)
    app.handle(ord("z"))
    app.handle(27)  # cancel
    assert app.editing is None and row.value == "A"


def test_ctrl_g_generates_a_hex_secret() -> None:
    """The generate shortcut fills a secret row with 64 hex characters."""
    app = _demo_app()
    app.focus = 2
    app.handle(7)

    row = app.current.rows[2]
    assert len(row.value) == 64
    assert all(char in "0123456789abcdef" for char in row.value)
    assert app.message_level == "ok"


def test_the_letter_g_also_generates_a_secret() -> None:
    """The discoverable shortcut works without Ctrl, matching the help text."""
    app = _demo_app()
    app.focus = 2
    app.handle(ord("g"))

    row = app.current.rows[2]
    assert len(row.value) == 64
    assert row.value != "abc123"


def test_choice_rows_cycle_and_open_a_picker() -> None:
    """Left/right cycle a choice; Enter opens the selection modal."""
    row = Row("choice", label="Método", value="uv", options=["uv", "venv", "skip"])
    app = App("test")
    app.push(Screen(title="x", rows=[row], footer=""))
    app.focus = 0

    app.handle(curses.KEY_RIGHT)
    assert row.value == "venv"
    app.handle(10)
    assert app.modal is not None
    app.handle(curses.KEY_DOWN)
    app.handle(10)
    assert app.modal is None and row.value == "skip"


def test_advanced_rows_stay_hidden_until_toggled() -> None:
    """The advanced toggle controls both focus and rendering."""
    row = Row("text", label="Avanzado", value="x", advanced=True)
    app = App("test")
    app.push(Screen(title="x", rows=[Row("text", label="Base", value=""), row], footer=""))

    assert [r.label for r in app.visible_rows()] == ["Base"]
    app.handle(ord("a"))
    assert app.show_advanced
    assert [r.label for r in app.visible_rows()] == ["Base", "Avanzado"]


def test_debian_theme_is_the_default() -> None:
    """The installer uses the Debian palette unless mono is requested."""
    from setup_wizard import curses_ui

    assert curses_ui.resolve_theme("debian").key == "debian"
    assert curses_ui.resolve_theme("auto").key == "debian"
    assert curses_ui.resolve_theme("dark").key == "debian"
    assert curses_ui.resolve_theme("mono").key == "mono"


def test_theme_hotkey_no_longer_cycles_palettes() -> None:
    """The removed t hotkey leaves the fixed Debian theme untouched."""
    app = _demo_app()
    app.handle(ord("t"))
    assert app.theme_key == "debian"
    assert app._theme.key == "debian"


def test_f1_opens_help_and_f10_asks_to_quit() -> None:
    """Function keys follow the Debian installer convention."""
    app = _demo_app()
    app.handle(curses.KEY_F1)
    assert app.modal is not None
    app.modal = None
    app.handle(curses.KEY_F10)
    assert app.modal is not None


def test_tab_commits_the_edit_and_moves_to_the_next_field() -> None:
    """Tab is a navigation key, not an alternative Enter."""
    app = _demo_app()
    row = app.current.rows[1]
    app.focus = 1
    app.handle(10)
    app.handle(ord("X"))
    app.handle(9)

    assert app.editing is None
    assert row.value == "X"
    assert app.current.rows[app.focus].label == "Clave"


def test_focused_help_is_not_rendered_below_the_row() -> None:
    """Contextual help lives in its own pane, so rows keep a stable height."""
    app = _demo_app()
    layout = app._build_layout(60)
    nombre = next(
        lines for index, _start, lines in layout if app.current.rows[index].label == "Nombre"
    )
    assert len(nombre) == 1
    assert app._focused_help() == "ayuda"


def test_wrap_text_breaks_long_messages_on_word_boundaries() -> None:
    """Wrapping keeps words whole, respects newlines and splits long tokens."""
    from setup_wizard.curses_ui import _wrap_text

    lines = _wrap_text("uno dos tres cuatro cinco seis", 10)
    assert all(len(line) <= 10 for line in lines)
    assert " ".join(lines) == "uno dos tres cuatro cinco seis"
    assert _wrap_text("línea uno\nlínea dos", 40) == ["línea uno", "línea dos"]
    assert _wrap_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert _wrap_text("   sangría conservada", 40) == ["   sangría conservada"]


def test_long_notes_take_multiple_visual_lines() -> None:
    """A long note expands the row layout instead of being truncated."""
    from setup_wizard.curses_ui import _wrap_text

    note = "palabra " * 30
    app = App("test")
    app.push(Screen(title="x", rows=[Row("note", label=note)], footer=""))

    layout = app._build_layout(30)
    assert len(layout[0][2]) == len(_wrap_text(note, 27)) > 1


def test_dialog_page_keys_scroll_long_messages() -> None:
    """Long dialogs can be scrolled so every wrapped line is reachable."""
    from setup_wizard.curses_ui import Dialog

    app = App("test")
    app.push(Screen(title="x", rows=[], footer=""))
    dialog = Dialog("Aviso", ["línea"] * 30)
    app.modal = dialog

    app.handle(curses.KEY_NPAGE)
    assert dialog.offset == 5
    app.handle(curses.KEY_PPAGE)
    assert dialog.offset == 0
    app.handle(curses.KEY_END)
    assert dialog.offset == 10**9


# -- screen flow ------------------------------------------------------------


@pytest.fixture
def wizard_context(tmp_path: Path) -> Context:
    """Context backed by miniature templates, never by the real repository."""
    return _context(_write_env_files(tmp_path))


def test_tui_starts_with_the_external_values_guide(wizard_context: Context) -> None:
    """The first screen explains what must be obtained outside the repository."""
    app = build_app(wizard_context)

    assert app.current.title == "Guía de valores externos"
    assert app.current.progress == "1/8"
    assert any("Fish Audio" in row.label for row in app.current.rows)


def test_external_guide_paginates_and_can_skip_to_the_menu(
    wizard_context: Context,
) -> None:
    """PgUp/PgDn, arrows and Home/End move between guide pages."""
    app = build_app(wizard_context)

    app.handle(curses.KEY_NPAGE)
    assert "Fish Audio" in app.current.subtitle
    assert app.current.progress == "2/8"
    app.handle(curses.KEY_PPAGE)
    assert "Antes de empezar" in app.current.subtitle
    app.handle(curses.KEY_END)
    assert app.current.progress == "8/8"
    app.handle(curses.KEY_HOME)
    assert app.current.progress == "1/8"

    skip = next(row for row in app.current.rows if row.label == "Saltar al menú")
    app.focus = app.current.rows.index(skip)
    app.handle(10)
    assert app.current.title == "Instalador de newsvoice"


def test_external_guide_covers_providers_urls_and_approvals() -> None:
    """The guide names the credentials, URLs and approvals an operator needs."""
    text = "\n".join(line for page in GUIDE_PAGES for line in page.lines)

    for expected in (
        "FISH_API_KEY",
        "HF_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SMTP_PASSWORD",
        "OPENAI_TTS_BASE_URL",
        "https://fish.audio/",
        "https://huggingface.co/kyutai/pocket-tts",
        "Agree and access repository",
        "https://t.me/BotFather",
        "https://myaccount.google.com/apppasswords",
    ):
        assert expected in text


def test_all_screens_build_usable_rows(wizard_context: Context) -> None:
    """Every screen the wizard can reach exposes focusable rows and buttons."""
    app = App("test")
    screens = [
        welcome_screen(app, wizard_context),
        prereq_screen(app, wizard_context, return_to="profile"),
        profile_screen(app, wizard_context),
        method_screen(app, wizard_context),
        env_screen(app, wizard_context, "deploy"),
        env_screen(app, wizard_context, "news"),
        env_screen(app, wizard_context, "gateway"),
    ]
    for screen in screens:
        focusable = [row for row in screen.rows if row.focusable]
        assert focusable, f"{screen.title} no tiene filas navegables"
        if screen.on_activate:
            assert any(row.kind == "button" for row in focusable), screen.title

    # Screens that rebuild themselves on enter must do so once pushed.
    app.push(review_screen(app, wizard_context))
    assert any(row.action == "write" for row in app.current.rows if row.kind == "button")
    app.push(apply_screen(app, wizard_context))
    assert any(row.action == "install" for row in app.current.rows if row.kind == "button")
    app.push(validate_screen(app, wizard_context, write_mode=True))
    assert app.current.title == "Paso 6 · validación"
    assert any(row.kind == "button" for row in app.current.rows)
    app.push(final_screen(app, wizard_context))
    assert any(row.action == "quit" for row in app.current.rows if row.kind == "button")


def test_review_screen_lists_pending_changes(wizard_context: Context) -> None:
    """The diff screen shows the exact keys that will be written."""
    wizard_context.envs["deploy"].set("API_PORT", "9001")
    app = App("test")
    app.push(review_screen(app, wizard_context))

    labels = [row.label for row in app.current.rows]
    assert any("API_PORT" in label and "9001" in label for label in labels)
    assert any("deploy/.env" in label for label in labels)


def test_env_screen_stages_edits_into_the_file(wizard_context: Context) -> None:
    """Committing an edit stages it, and the next screen sees it."""
    app = App("test")
    screen = env_screen(app, wizard_context, "deploy")
    app.push(screen)

    row = next(r for r in app.current.rows if r.label == "API news")
    app.focus = app.current.rows.index(row)
    app.handle(10)  # edit
    for _ in range(len(row.value)):
        app.handle(curses.KEY_BACKSPACE)
    for char in "7777":
        app.handle(ord(char))
    app.handle(10)  # commit

    assert wizard_context.envs["deploy"].get("API_PORT") == "7777"
    assert ("API_PORT", "8000", "7777") in wizard_context.envs["deploy"].diff_against_disk()


def test_f1_opens_the_detailed_field_reference(wizard_context: Context) -> None:
    """The env screen replaces the generic help with per-field documentation."""
    app = App("test")
    app.push(env_screen(app, wizard_context, "news"))

    app.handle(curses.KEY_F1)

    assert app.modal is not None
    text = "\n".join(app.modal.lines)
    assert "Endpoint OpenAI-compatible" in text
    assert "LLM_API_BASE" in text
    assert "obligatorio" in text
    assert "externo · obligatorio" in text
    assert "interno · opcional" in text
    assert "actual:" in text
    assert "── LLM ──" in text or "── LLM" in text


def test_fields_mark_whether_they_are_internal_or_external() -> None:
    """TTS, voices and service ports are internal; LLM and providers are external."""
    news = {field_.key: field_ for field_ in fields_for("news")}
    gateway = {field_.key: field_ for field_ in fields_for("gateway")}

    assert news["LLM_API_BASE"].source == "externo"
    assert news["TTS_VOICE"].source == "interno"
    assert news["API_PORT"].source == "interno"
    assert gateway["FISH_API_KEY"].source == "externo"
    assert gateway["TTS_DEVICE"].source == "interno"


def test_env_screen_rows_carry_the_internal_or_external_badge(
    wizard_context: Context,
) -> None:
    """The data entry rows expose the source without changing their label."""
    app = App("test")
    screen = env_screen(app, wizard_context, "news")

    llm = next(row for row in screen.rows if row.label == "Base LLM")
    voice = next(row for row in screen.rows if row.label == "Voz TTS")

    assert llm.source == "externo"
    assert voice.source == "interno"
    assert any("[interno] lo aporta el stack" in row.label for row in screen.rows)
    rendered = "".join(text for _x, text, _attr in app._row_visual_lines(llm, 80, False)[0])
    assert "[externo]" in rendered


def test_gateway_screen_shows_fish_fields_without_advanced_toggle(
    wizard_context: Context,
) -> None:
    """Fish credentials are common enough to be visible in the default view."""
    app = App("test")
    app.push(env_screen(app, wizard_context, "gateway"))

    labels = [row.label for row in app.visible_rows()]
    assert "Fish API key" in labels
    assert "Modelo Fish" in labels


def test_saving_the_last_env_file_opens_the_review_screen(
    wizard_context: Context,
) -> None:
    """Saving gateway, the last file, must not index past ``_ORDER``."""
    env = wizard_context.envs["gateway"]
    env.set("FISH_API_KEY", "fish-test")
    app = App("test")
    app.push(env_screen(app, wizard_context, "gateway"))

    button = next(r for r in app.current.rows if r.label == "Guardar en disco y continuar")
    app.focus = app.current.rows.index(button)
    app.handle(10)

    assert "Paso 4" in app.current.title
    assert "FISH_API_KEY=fish-test" in env.path.read_text(encoding="utf-8")


def test_save_dialogs_start_on_the_action_button(wizard_context: Context) -> None:
    """Enter on a save confirmation writes instead of cancelling."""
    wizard_context.envs["deploy"].set("API_PORT", "9100")

    one = _confirm_write_one(App("test"), wizard_context, "deploy")
    every = _confirm_write(App("test"), wizard_context)

    assert one is not None and one.index == 1
    assert every.index == 1


def test_guardar_y_continuar_writes_the_file_before_advancing(
    wizard_context: Context,
) -> None:
    """The wizard save button now persists the current file immediately."""
    env = wizard_context.envs["deploy"]
    env.set("API_PORT", "9200")
    app = App("test")
    app.push(env_screen(app, wizard_context, "deploy"))

    button = next(r for r in app.current.rows if r.label == "Guardar en disco y continuar")
    app.focus = app.current.rows.index(button)
    app.handle(10)

    assert env.path.exists()
    assert "API_PORT=9200" in env.path.read_text(encoding="utf-8")
    assert env.changed_keys() == []
    assert env.key_path in wizard_context.written
    assert "news" in app.current.title


def test_write_error_is_reported_without_leaving_the_tui(
    wizard_context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission errors become a modal instead of crashing into line mode."""
    env = wizard_context.envs["deploy"]
    env.set("API_PORT", "9300")

    def fail(*_args, **_kwargs):
        raise OSError("permiso denegado")

    monkeypatch.setattr(env, "write", fail)
    app = App("test")

    ok, backup, names = _write_env(app, env, reload=True)

    assert not ok and backup is None and names == ["API_PORT"]
    assert app.message_level == "bad"
    assert app.modal is not None and app.modal.title == "Error al guardar"
    assert "permiso denegado" in "\n".join(app.modal.lines)


def test_tui_error_after_start_does_not_fall_back_to_line_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after curses starts must not re-prompt values in plain mode."""
    from setup_wizard import __main__ as cli
    from setup_wizard import curses_ui, plain

    class StartedApp:
        started = True

    def fail(_app):
        raise RuntimeError("boom")

    called: list[int] = []
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "load_context", lambda: object())
    monkeypatch.setattr(cli, "build_app", lambda _ctx: StartedApp())
    monkeypatch.setattr(curses_ui, "run_tui", fail)
    monkeypatch.setattr(plain, "run_plain", lambda *_a, **_k: called.append(1) or 0)

    assert cli.main([]) == 1
    assert called == []


def test_profile_defaults_do_not_clobber_operator_values(wizard_context: Context) -> None:
    """Applying a profile only seeds values that look untouched."""
    from setup_wizard.app import _apply_profile

    wizard_context.profile = next(p for p in PROFILES if p.key == "local")
    wizard_context.envs["news"].set("LLM_API_BASE", "http://10.0.0.5:9000/v1")

    _apply_profile(wizard_context)

    assert wizard_context.envs["news"].get("LLM_API_BASE") == "http://10.0.0.5:9000/v1"
    assert wizard_context.envs["news"].get("TTS_API_BASE_URL") == "http://localhost:8001/api/v1"


def test_shared_gateway_key_is_propagated(wizard_context: Context) -> None:
    """The deploy key is copied to both consumers when they are unset."""
    from setup_wizard.app import _sync_shared_key

    wizard_context.envs["deploy"].set("GATEWAY_API_KEY", "k" * 64)
    _sync_shared_key(wizard_context)

    assert wizard_context.envs["news"].get("TTS_API_KEY") == "k" * 64
    assert wizard_context.envs["gateway"].get("API_KEY") == "k" * 64


# -- line mode --------------------------------------------------------------


def test_plain_mode_writes_the_three_files(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A scripted run applies the profile, writes everything and validates."""
    ctx = _context(_write_env_files(tmp_path))

    status = run_plain(
        ctx,
        method_hint="skip",
        profile_hint="docker",
        assume_yes=True,
    )

    assert status == 0, capsys.readouterr().out
    news_env = ctx.envs["news"]
    assert news_env.exists
    assert news_env.get("TTS_API_BASE_URL") == "http://gateway:8000/api/v1"
    assert ctx.envs["deploy"].exists
    assert ctx.envs["gateway"].exists
    assert ctx.written
    out = capsys.readouterr().out
    assert "Paso 6 · validación" in out
    assert "comprobaciones" in out


def test_plain_mode_leaves_files_alone_in_audit_profile(tmp_path: Path) -> None:
    """The audit profile only inspects, even with every confirmation given."""
    envs = _write_env_files(tmp_path)
    ctx = _context(envs)

    run_plain(ctx, method_hint="skip", profile_hint="audit", assume_yes=True)

    assert not any(target.exists for target in ctx.envs.values())


def test_real_repository_templates_parse(tmp_path: Path) -> None:
    """The shipped templates parse and expose every declared field."""
    repo = Path(__file__).resolve().parents[4]
    for file_key, rel in (
        ("deploy", "deploy/.env.example"),
        ("news", "apps/news/.env.example"),
        ("gateway", "apps/gateway/.env.example"),
    ):
        source = repo / rel
        assert source.exists(), rel
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        env = EnvFile.load(target, source)
        for field in fields_for(file_key):
            assert env.has(field.key), f"{rel} no menciona {field.key}"


def test_load_context_reads_the_repository() -> None:
    """The default context loads the real env files without touching them."""
    before = {
        env.path: env.path.stat().st_mtime_ns if env.path.exists() else 0
        for env in load_context().envs.values()
    }
    ctx = load_context()
    assert set(ctx.envs) == {"deploy", "news", "gateway"}
    assert isinstance(ctx.report.python, str) and ctx.report.python
    for env in ctx.envs.values():
        after = env.path.stat().st_mtime_ns if env.path.exists() else 0
        assert after == before[env.path]


# -- system prerequisites -----------------------------------------------------


def _report_with(*findings: Finding) -> Report:
    """Build a detection report holding only the given findings."""
    report = Report(python="3.12.0")
    report.findings.extend(findings)
    return report


def test_missing_optional_tool_offers_its_install_command() -> None:
    """A missing tool is listed with the command that provides it."""
    items = {
        item.key: item
        for item in prerequisites(_report_with(Finding("uv", "warn", "no está en PATH (opcional)")))
    }

    assert items["uv"].missing is True
    assert items["uv"].required is False
    assert items["uv"].automated is True
    assert items["uv"].symbol == "⚠"
    assert any("astral.sh/uv" in command for command in items["uv"].remedy)


def test_missing_python_is_blocking_and_manual() -> None:
    """Python cannot be self-installed, so the wizard only explains it."""
    items = {
        item.key: item
        for item in prerequisites(
            _report_with(Finding("Python", "bad", "3.10 es demasiado viejo: hace falta 3.11+"))
        )
    }

    assert items["python"].required is True
    assert items["python"].remedy == ()
    assert items["python"].automated is False
    assert items["python"].symbol == "✗"


def test_prerequisite_steps_wrap_every_command_in_a_shell() -> None:
    """Remedies may contain pipes, so they run through ``bash -lc``."""
    steps = deps.prereq_steps(
        prerequisites(_report_with(Finding("ffmpeg", "bad", "ffmpeg no está en PATH")))
    )

    assert steps, "ffmpeg tiene comando de instalación"
    assert all(step.command[:2] == ["bash", "-lc"] for step in steps)
    assert any("apt-get install -y ffmpeg" in step.command[2] for step in steps)
    assert all(step.env and "PATH" in step.env for step in steps)


def test_prereq_screen_lists_the_missing_tool_and_its_command(
    wizard_context: Context,
) -> None:
    """The screen explains why each tool is needed and how to get it."""
    wizard_context.report = Report(
        python="3.12.0",
        findings=[Finding("uv", "warn", "no está en PATH (opcional)")],
    )
    app = App("test")
    screen = prereq_screen(app, wizard_context, return_to="profile")

    labels = [row.label for row in screen.rows]
    assert any("uv" in label for label in labels)
    assert any("hace falta porque" in label for label in labels)
    assert any("$ curl -LsSf https://astral.sh/uv" in label for label in labels)
    assert any(row.action == "install" for row in screen.rows if row.kind == "button")


# -- shared gateway key -------------------------------------------------------


def test_editing_any_gateway_key_updates_the_other_two_files(
    wizard_context: Context,
) -> None:
    """Typing the key in one file stages the same value in the other two."""
    app = App("test")
    app.push(env_screen(app, wizard_context, "deploy"))
    row = next(r for r in app.current.rows if r.label == "Clave del gateway")
    app.focus = app.current.rows.index(row)
    app.handle(10)  # start editing
    for char in "aabbccddeeff00112233445566778899":
        app.handle(ord(char))
    app.handle(10)  # commit

    assert wizard_context.envs["deploy"].get("GATEWAY_API_KEY").startswith("aabb")
    assert wizard_context.envs["news"].get("TTS_API_KEY") == (
        wizard_context.envs["deploy"].get("GATEWAY_API_KEY")
    )
    assert wizard_context.envs["gateway"].get("API_KEY") == (
        wizard_context.envs["deploy"].get("GATEWAY_API_KEY")
    )


def test_conflicting_gateway_keys_are_reported_and_unified(
    wizard_context: Context,
) -> None:
    """Diverging copies are listed and collapsed onto one value."""
    wizard_context.envs["deploy"].set("GATEWAY_API_KEY", "aaaa")
    wizard_context.envs["news"].set("TTS_API_KEY", "bbbb")
    wizard_context.envs["gateway"].set("API_KEY", "cccc")

    assert _shared_key_conflicts(wizard_context) == [
        "news/TTS_API_KEY",
        "gateway/API_KEY",
    ]
    assert _unify_shared_key(wizard_context) == 2
    assert _shared_key_conflicts(wizard_context) == []
    assert wizard_context.envs["news"].get("TTS_API_KEY") == "aaaa"
    assert wizard_context.envs["gateway"].get("API_KEY") == "aaaa"


def test_validate_flags_consumers_that_disagree_without_deploy_key(
    tmp_path: Path,
) -> None:
    """Even with deploy/.env empty, two different consumers are an error."""
    envs = _write_env_files(tmp_path)
    envs["news"].set("TTS_API_KEY", "aaa")
    envs["gateway"].set("API_KEY", "bbb")

    checks = validation.validate(envs, Report(python="3.12.0"), will_write=True)
    by_name = {check.name: check for check in checks}

    assert by_name["clave compartida"].status == "bad"
    assert "news.TTS_API_KEY" in by_name["clave compartida"].detail
    assert "gateway.API_KEY" in by_name["clave compartida"].detail


# -- docker client vs daemon (WSL2) -------------------------------------------


def test_docker_status_reports_the_daemon_when_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable daemon is reported with the server version."""
    from setup_wizard import detect as detect_mod

    def fake_tool_version(command: list[str], timeout: float = 10.0) -> str | None:
        if "--version" in command:
            return "Docker version 29.0.0, build abc"
        if any("Server.Version" in part for part in command):
            return "29.0.0"
        return None

    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "tool_version", fake_tool_version)

    status, detail = detect_mod._docker_status()

    assert status == "ok"
    assert "29.0.0" in detail


def test_docker_status_separates_client_from_daemon_on_wsl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`docker --version` works while the daemon is unreachable on WSL2."""
    from setup_wizard import detect as detect_mod

    def fake_tool_version(command: list[str], timeout: float = 10.0) -> str | None:
        return "Docker version 29.0.0, build abc" if "--version" in command else None

    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "tool_version", fake_tool_version)
    monkeypatch.setattr(detect_mod, "_is_wsl", lambda: True)
    monkeypatch.setattr(detect_mod, "_docker_candidates", lambda: [])
    monkeypatch.setattr(detect_mod, "_docker_failure_reason", lambda: "")
    monkeypatch.setattr(detect_mod, "_docker_desktop_integration", lambda: False)

    status, detail = detect_mod._docker_status()

    assert status == "warn"
    assert "Docker version 29.0.0" in detail
    assert "WSL Integration" in detail


def test_docker_status_points_at_a_shadowing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snap/apt docker in PATH shadowing Desktop's working client is named."""
    from setup_wizard import detect as detect_mod

    def fake_tool_version(command: list[str], timeout: float = 10.0) -> str | None:
        if command[:1] == ["docker"]:
            return "Docker version 29.0.0, build abc" if "--version" in command else None
        if any("Server.Version" in part for part in command):
            return "29.0.0"
        return None

    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/snap/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "tool_version", fake_tool_version)
    monkeypatch.setattr(
        detect_mod, "_docker_candidates", lambda: ["/snap/bin/docker", "/usr/bin/docker"]
    )
    monkeypatch.setattr(detect_mod, "_docker_failure_reason", lambda: "no escucha")

    status, detail = detect_mod._docker_status()

    assert status == "warn"
    assert "/snap/bin/docker" in detail
    assert "/usr/bin/docker" in detail
    assert "sombrea" in detail


def test_docker_status_blames_the_engine_when_integration_is_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop's integration mount exists, so the engine (not the toggle) failed."""
    from setup_wizard import detect as detect_mod

    def fake_tool_version(command: list[str], timeout: float = 10.0) -> str | None:
        return "Docker version 29.0.0, build abc" if "--version" in command else None

    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "tool_version", fake_tool_version)
    monkeypatch.setattr(detect_mod, "_docker_candidates", lambda: [])
    monkeypatch.setattr(detect_mod, "_docker_failure_reason", lambda: "no escucha")
    monkeypatch.setattr(detect_mod, "_docker_desktop_integration", lambda: True)

    status, detail = detect_mod._docker_status()

    assert status == "warn"
    assert "Engine running" in detail
    assert "integración de Docker Desktop está montada" in detail


def test_docker_status_offers_the_group_remedy_on_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket that refuses the handshake points at the docker group."""
    from setup_wizard import detect as detect_mod

    def fake_tool_version(command: list[str], timeout: float = 10.0) -> str | None:
        return "Docker version 29.0.0, build abc" if "--version" in command else None

    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "tool_version", fake_tool_version)
    monkeypatch.setattr(detect_mod, "_docker_candidates", lambda: [])
    monkeypatch.setattr(
        detect_mod, "_docker_failure_reason", lambda: "permiso denegado en el socket"
    )
    monkeypatch.setattr(detect_mod, "_docker_desktop_integration", lambda: False)

    status, detail = detect_mod._docker_status()

    assert status == "warn"
    assert "grupo docker" in detail
    assert "usermod -aG docker" in detail


def test_docker_prerequisite_offers_the_group_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission failure is the one WSL/docker case a command can fix."""
    from setup_wizard import detect as detect_mod

    report = _report_with(
        Finding("docker", "warn", "cliente docker, permiso denegado: grupo docker")
    )
    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )

    items = {item.key: item for item in detect_mod.prerequisites(report)}

    assert items["docker"].remedy == ("sudo usermod -aG docker $USER",)
    assert items["docker"].automated is True


def test_docker_prerequisite_is_manual_on_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker Desktop is toggled from its GUI, so the wizard offers no command."""
    from setup_wizard import detect as detect_mod

    report = _report_with(Finding("docker", "warn", "cliente docker instalado"))
    monkeypatch.setattr(
        detect_mod, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(detect_mod, "_is_wsl", lambda: True)

    items = {item.key: item for item in detect_mod.prerequisites(report)}

    assert items["docker"].missing is True
    assert items["docker"].remedy == ()
    assert items["docker"].automated is False
