"""Screen flow of the guided setup: profile, env files, install, validation.

The screens only build :class:`~curses_ui.Row` lists and react to buttons;
all the data rules live in :mod:`settings`, :mod:`deps` and :mod:`validate`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import deps
from . import validate as validation
from .curses_ui import App, Row, Screen
from .detect import Report, detect
from .envfile import EnvFile
from .settings import PROFILES, Field, Profile
from .util import REPO_ROOT, env_paths

__all__ = ["Context", "build_app", "load_context"]

_ORDER = ("deploy", "news", "gateway")


@dataclass
class Context:
    """State shared by every screen.

    Attributes:
        report: Last environment detection.
        envs: The three env files, already loaded from disk or template.
        profile: Selected starting point.
        method: Dependency installation method id.
        written: Env paths written during this session.
        install_result: Outcome of the last installation attempt.
    """

    report: Report
    envs: dict[str, EnvFile] = field(default_factory=dict)
    profile: Profile = PROFILES[0]
    method: str = "uv"
    written: list[str] = field(default_factory=list)
    install_result: str = ""


def load_context(report: Report | None = None) -> Context:
    """Load the three env files and detect the host.

    Args:
        report: Pre-computed detection, or ``None`` to run it now.

    Returns:
        A ready to use :class:`Context`.
    """
    report = report or detect()
    envs = {target.key: EnvFile.load(target.path, target.example) for target in env_paths()}
    return Context(report=report, envs=envs)


# -- small helpers ----------------------------------------------------------


def _mask(value: str) -> str:
    """Hide the body of a secret while keeping its shape visible."""
    if not value:
        return "(vacío)"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}…{value[-3:]} ({len(value)} caracteres)"


_SECRET_KEYS = frozenset(
    {
        "GATEWAY_API_KEY",
        "TTS_API_KEY",
        "API_KEY",
        "ADMIN_API_TOKEN",
        "ADMIN_PASSWORD_HASH",
        "DOC_TTS_LINK_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "SMTP_PASSWORD",
        "HF_TOKEN",
    }
)


def _value_for(ctx: Context, field_: Field) -> str:
    """Return the effective value of a field for the UI."""
    current = ctx.envs[field_.file].get(field_.key).strip()
    if current:
        return current
    return field_.default


def _apply_profile(ctx: Context) -> None:
    """Seed untouched keys with the selected profile defaults.

    A key counts as untouched when it is unset in the file (including a
    commented template line) or still holds the template default, so a value
    the operator wrote by hand is never clobbered.
    """
    for (file_key, key), value in ctx.profile.defaults.items():
        env = ctx.envs.get(file_key)
        if env is None:
            continue
        field_ = next((f for f in _fields(file_key) if f.key == key), None)
        if field_ is None:
            continue
        current = env.get(key)
        if env.staged(key) is not None and current != field_.default:
            continue  # the operator typed a value during this session
        if env.original(key) and env.original(key) != field_.default:
            continue  # the file already carries a hand written value
        env.set(key, value)


def _fields(file_key: str) -> list[Field]:
    from .settings import fields_for

    return fields_for(file_key)


def _sync_shared_key(ctx: Context) -> None:
    """Copy ``deploy/GATEWAY_API_KEY`` into the two consumers when unset."""
    shared = ctx.envs["deploy"].get("GATEWAY_API_KEY").strip()
    if not shared:
        return
    for file_key, key in (("news", "TTS_API_KEY"), ("gateway", "API_KEY")):
        env = ctx.envs[file_key]
        if env.get(key).strip() == shared:
            continue
        if env.original(key) and env.original(key) != shared:
            continue  # operator wrote a different value on purpose
        env.set(key, shared)


def _changes(ctx: Context) -> dict[str, list[tuple[str, str, str]]]:
    """Return the pending diff of every env file."""
    return {key: ctx.envs[key].diff_against_disk() for key in _ORDER}


def _emit_factory(app: App):
    """Build the progress callback used while commands run."""

    def emit(status: str, title: str, detail: str) -> None:
        symbol = {"run": "▸", "ok": "✓", "bad": "✗"}.get(status, "·")
        level = {"ok": "ok", "bad": "bad"}.get(status, "")
        app.say(f"{symbol} {title}: {detail}"[:200], level)
        app.redraw()

    return emit


# -- screens ----------------------------------------------------------------


def welcome_screen(app: App, ctx: Context) -> Screen:
    """Build the landing screen with the detected environment."""
    rows: list[Row] = [
        Row("heading", label="Bienvenido a newsvoice"),
        Row(
            "note", label="Instalador guiado: prepara los tres .env y las dependencias.", result=""
        ),
        Row("note", label=""),
    ]
    rows.append(Row("note", label=f"Python {ctx.report.python}", result="ok"))
    for finding in ctx.report.findings:
        if finding.name.startswith(".env"):
            rows.append(
                Row("note", label=f"{finding.name}: {finding.detail}", result=finding.status)
            )
    for finding in ctx.report.findings:
        if finding.name in ("uv", "docker", "ffmpeg", "GPU", "virtualenvs", "compose"):
            rows.append(
                Row("note", label=f"{finding.name}: {finding.detail}", result=finding.status)
            )
    if ctx.report.failures:
        rows.append(
            Row(
                "note",
                label=(
                    f"{ctx.report.failures} requisito(s) bloqueante(s): "
                    "el asistente te dirá cómo arreglarlos."
                ),
                result="bad",
            )
        )
    rows.append(Row("note", label=""))
    rows.extend(
        [
            Row("button", label="Asistente guiado (.env + dependencias)", action="wizard"),
            Row("button", label="Volver a detectar el entorno", action="redetect"),
            Row("button", label="Validar la configuración actual", action="audit"),
            Row("button", label="Ayuda de teclado", action="help"),
            Row("button", label="Salir", action="quit"),
        ]
    )

    def activate(app_: App, row: Row) -> None:
        if row.action == "wizard":
            app_.say("Elige el punto de partida")
            app_.push(profile_screen(app_, ctx))
        elif row.action == "redetect":
            ctx.report = detect()
            app_.say("Entorno actualizado", "ok")
            app_.replace(welcome_screen(app_, ctx))
        elif row.action == "audit":
            app_.push(validate_screen(app_, ctx, write_mode=False))
        elif row.action == "help":
            app_.info(
                "Ayuda",
                "↑/↓ mover · Enter editar/abrir · Esc atrás",
                "Tab siguiente · a mostrar avanzados",
                "g generar secreto · Ctrl+R valor por defecto",
                "q salir del instalador",
            )
        elif row.action == "quit":
            app_.ask_quit()

    return Screen(
        title="newsvoice",
        subtitle="instalador guiado",
        rows=rows,
        footer="↑/↓  mover    Enter  ejecutar    ?  ayuda    q  salir",
        on_activate=activate,
    )


def profile_screen(app: App, ctx: Context) -> Screen:
    """Build the deployment shape picker."""
    labels = [profile.label for profile in PROFILES]
    by_label = {profile.label: profile for profile in PROFILES}
    ctx.profile = by_label.get(ctx.profile.label, PROFILES[0])

    desc = Row("note", label=ctx.profile.description, result="ok")
    rows = [
        Row("heading", label="Punto de partida"),
        Row("note", label="Define los valores por defecto; todo es editable después."),
        Row("note", label=""),
        Row(
            "choice",
            label="Perfil",
            value=ctx.profile.label,
            options=labels,
            help="←/→ o Enter para cambiar de perfil",
        ),
        desc,
        Row("note", label=""),
        Row("button", label="Continuar con este perfil", action="next"),
        Row("button", label="Atrás", action="back"),
    ]

    def on_change(app_: App, row: Row) -> None:
        profile = by_label.get(row.value)
        if profile is None:
            return
        ctx.profile = profile
        desc.label = profile.description

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "next":
            if ctx.profile.key == "audit":
                app_.say("Modo solo revisión: no se escribe nada")
                app_.push(validate_screen(app_, ctx, write_mode=False))
                return
            _apply_profile(ctx)
            app_.say(f"Perfil {ctx.profile.label}", "ok")
            app_.push(method_screen(app_, ctx))

    return Screen(
        title="Paso 1 · perfil",
        subtitle=ctx.profile.key,
        rows=rows,
        footer="←/→ cambiar perfil    Enter continuar    Esc atrás",
        on_change=on_change,
        on_activate=activate,
    )


def method_screen(app: App, ctx: Context) -> Screen:
    """Build the dependency installation picker."""
    options = [label for _key, label, _detail in deps.METHODS]
    by_label = {label: _key for _key, label, _detail in deps.METHODS}
    detail_by_label = {label: detail for _key, label, detail in deps.METHODS}
    current = next((label for key, label, _d in deps.METHODS if key == ctx.method), options[0])

    desc = Row("note", label=detail_by_label[current], result="ok")
    rows = [
        Row("heading", label="Instalación de dependencias"),
        Row("note", label="Los comandos se muestran antes de ejecutarse."),
        Row("note", label=""),
        Row("choice", label="Método", value=current, options=options, help="←/→ para cambiar"),
        desc,
        Row("note", label=""),
        Row("button", label="Continuar a los archivos .env", action="next"),
        Row("button", label="Atrás", action="back"),
    ]

    def on_change(app_: App, row: Row) -> None:
        key = by_label.get(row.value, "uv")
        ctx.method = key
        desc.label = detail_by_label.get(row.value, "")
        available, reason = deps.method_available(key)
        desc.result = "ok" if available else "warn"
        if not available:
            desc.label = f"{detail_by_label.get(row.value, '')} — {reason}"

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "next":
            app_.push(env_screen(app_, ctx, "deploy"))

    return Screen(
        title="Paso 2 · dependencias",
        subtitle=ctx.method,
        rows=rows,
        footer="←/→ cambiar método    Enter continuar    Esc atrás",
        on_change=on_change,
        on_activate=activate,
    )


def env_screen(app: App, ctx: Context, file_key: str) -> Screen:
    """Build the form for one env file, sections included."""
    from .settings import sections_for as _sections

    env = ctx.envs[file_key]
    target = next(t for t in env_paths() if t.key == file_key)
    rows: list[Row] = [
        Row("heading", label=f"{target.label}"),
        Row(
            "note",
            label=f"{target.key_path} — "
            + ("se creará desde su plantilla" if not env.exists else "ya existe"),
            result="ok" if env.exists else "warn",
        ),
        Row("note", label=""),
    ]
    for section in _sections(file_key):
        rows.append(Row("heading", label=section.title))
        for field_ in section.fields:
            value = _value_for(ctx, field_)
            rows.append(
                Row(
                    kind="password" if field_.kind == "password" else field_.kind,
                    label=field_.label,
                    value=value,
                    help=field_.help,
                    options=list(field_.options),
                    default=field_.default,
                    generate=field_.generate,
                    advanced=field_.advanced,
                    secret=field_.kind in ("secret", "password"),
                )
            )
        rows.append(Row("note", label=""))
    rows.extend(
        [
            Row("button", label="Ver cambios de este archivo", action="diff"),
            Row("button", label="Guardar y continuar", action="next"),
            Row("button", label="Atrás", action="back"),
        ]
    )

    def on_change(app_: App, row: Row) -> None:
        field_ = _find_field(file_key, row)
        if field_ is None:
            return
        ctx.envs[file_key].set(field_.key, row.value)

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "diff":
            pending = env.diff_against_disk()
            if not pending:
                app_.info(f"{target.key_path}", "Sin cambios pendientes en este archivo.")
            else:
                lines = [
                    f"{key}: {_shown(key, old)} → {_shown(key, new)}" for key, old, new in pending
                ]
                app_.info(f"{target.key_path} · {len(pending)} cambio(s)", *lines)
            return
        if row.action == "next":
            _sync_shared_key(ctx)
            nxt = _ORDER[_ORDER.index(file_key) + 1] if file_key in _ORDER else None
            if nxt:
                app_.push(env_screen(app_, ctx, nxt))
            else:
                app_.push(review_screen(app_, ctx))

    return Screen(
        title=f"Paso 3 · {file_key}/.env",
        subtitle=target.key_path,
        rows=rows,
        footer="Enter editar · g generar secreto · a avanzados · Esc atrás",
        on_change=on_change,
        on_activate=activate,
    )


def _find_field(file_key: str, row: Row) -> Field | None:
    for field_ in _fields(file_key):
        if field_.label == row.label:
            return field_
    return None


def _shown(key: str, value: str) -> str:
    return _mask(value) if key in _SECRET_KEYS else (value or "(vacío)")


def review_screen(app: App, ctx: Context) -> Screen:
    """Build the pending diff summary. Rebuilt every time it is entered."""

    def build() -> list[Row]:
        rows = [
            Row("heading", label="Revisa los cambios"),
            Row("note", label="Nada se escribe hasta que lo confirmes."),
            Row("note", label=""),
        ]
        total = 0
        for key in _ORDER:
            changes = ctx.envs[key].diff_against_disk()
            rows.append(
                Row(
                    "note",
                    label=f"{ctx.envs[key].key_path}: {len(changes)} cambio(s)",
                    result="ok" if changes else "",
                )
            )
            for name, old, new in changes:
                rows.append(
                    Row("note", label=f"  {name}: {_shown(name, old)} → {_shown(name, new)}")
                )
                total += 1
            rows.append(Row("note", label=""))
        rows.append(Row("note", label=f"Total: {total} valor(es) por escribir."))
        rows.append(Row("note", label=""))
        rows.extend(
            [
                Row("button", label="Escribir los archivos .env", action="write"),
                Row("button", label="Validar sin escribir todavía", action="validate"),
                Row("button", label="Atrás", action="back"),
            ]
        )
        return rows

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=False))
            return
        if row.action == "write":
            app_.modal = _confirm_write(app_, ctx)

    return Screen(
        title="Paso 4 · revisión",
        subtitle="diff de los tres archivos",
        rows=build(),
        footer="Enter escribir · Esc atrás",
        on_enter=on_enter,
        on_activate=activate,
    )


def _confirm_write(app: App, ctx: Context):
    """Return the confirmation dialog that writes the env files."""
    from .curses_ui import Dialog

    def do_write(app_: App) -> None:
        app_.close_modal()
        written: list[str] = []
        for key in _ORDER:
            env = ctx.envs[key]
            if not env.changed_keys() and env.exists:
                continue
            backup = env.write()
            written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
        ctx.written = written
        if written:
            app_.say(f"Escrito: {', '.join(written)}", "ok")
        else:
            app_.say("No había nada que escribir", "ok")
        app_.push(apply_screen(app_, ctx))

    changes = sum(len(ctx.envs[key].diff_against_disk()) for key in _ORDER)
    return Dialog(
        "Escribir archivos",
        [
            f"Se escribirán hasta {changes} valor(es) en {len(_ORDER)} archivo(s).",
            "Las copias previas guardan .bak-<fecha>.",
        ],
        [("Cancelar", lambda a: a.close_modal()), ("Escribir", do_write)],
    )


def apply_screen(app: App, ctx: Context) -> Screen:
    """Build the dependency installation step."""

    def build() -> list[Row]:
        steps = deps.plan(ctx.method)
        rows = [
            Row("heading", label="Instalación"),
            Row("note", label=f"Método: {ctx.method}"),
            Row("note", label=""),
        ]
        if not steps:
            rows.append(
                Row("note", label="No se instalará nada (método «más tarde»).", result="warn")
            )
        for step in steps:
            rows.append(Row("note", label=f"▸ {step.title} — {' '.join(map(str, step.command))}"))
        if ctx.report.venvs:
            rows.append(
                Row(
                    "note",
                    label=f"virtualenvs actuales: {', '.join(ctx.report.venvs)}",
                    result="ok",
                )
            )
        rows.append(Row("note", label=""))
        rows.extend(
            [
                Row("button", label="Instalar dependencias", action="install"),
                Row("button", label="Validar configuración", action="validate"),
                Row("button", label="Instrucciones de despliegue", action="cmds"),
                Row("button", label="Atrás", action="back"),
            ]
        )
        if ctx.install_result:
            rows.insert(3, Row("note", label=ctx.install_result, result="ok"))
        return rows

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=bool(ctx.written)))
            return
        if row.action == "cmds":
            app_.info(
                "Despliegue",
                "make up          · levanta el stack completo",
                "make status      · salud de los 13 servicios",
                "make deploy      · scripts/deploy.sh production",
                "GPU=1 make up    · añade deploy/docker-compose.gpu.yml",
            )
            return
        if row.action != "install":
            return
        available, reason = deps.method_available(ctx.method)
        if not available:
            app_.say(reason, "bad")
            return
        steps = deps.plan(ctx.method)
        if not steps:
            app_.say("Nada que instalar con este método")
            return
        emit = _emit_factory(app_)
        app_.say("Instalando…")
        app_.redraw()
        ok = deps.execute(steps, emit)
        if ok and deps.verify(ctx.method):
            ok = deps.execute(deps.verify(ctx.method), emit)
        ctx.install_result = (
            "Instalación correcta" if ok else "Instalación con fallos (revisa el mensaje)"
        )
        if ok:
            ctx.report = detect()
        app_.say(ctx.install_result, "ok" if ok else "bad")
        app_.current.rows = build()
        app_.refocus()

    return Screen(
        title="Paso 5 · dependencias",
        subtitle=ctx.method,
        rows=build(),
        footer="Enter ejecutar · Esc atrás",
        on_enter=on_enter,
        on_activate=activate,
    )


def validate_screen(app: App, ctx: Context, *, write_mode: bool) -> Screen:
    """Build the validation screen from the shared rules."""

    def build() -> list[Row]:
        checks = validation.validate(
            ctx.envs, ctx.report, will_write=write_mode, profile=ctx.profile.key
        )
        bad = sum(1 for check in checks if check.status == "bad")
        warn = sum(1 for check in checks if check.status == "warn")
        rows = [
            Row("heading", label="Validación"),
            Row(
                "note",
                label=f"{len(checks)} comprobaciones · {bad} errores · {warn} avisos",
                result="bad" if bad else ("warn" if warn else "ok"),
            ),
            Row("note", label=""),
        ]
        for check in checks:
            rows.append(Row("note", label=f"{check.name}: {check.detail}", result=check.status))
        rows.append(Row("note", label=""))
        rows.extend(
            [
                Row("button", label="Volver a validar", action="again"),
                Row("button", label="Instrucciones de despliegue", action="cmds"),
                Row("button", label="Terminar", action="finish"),
                Row("button", label="Atrás", action="back"),
            ]
        )
        return rows

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "again":
            ctx.report = detect()
            app_.current.rows = build()
            app_.refocus()
            app_.say("Validación actualizada")
            return
        if row.action == "cmds":
            app_.info(
                "Despliegue",
                "make up          · levanta el stack completo",
                "make status      · salud de los 13 servicios",
                "make deploy      · scripts/deploy.sh production",
                "make urls        · puertos del panel, Temporal y gateway",
            )
            return
        if row.action == "finish":
            app_.push(final_screen(app_, ctx))

    return Screen(
        title="Paso 6 · validación",
        subtitle="reglas compartidas por TUI y CLI",
        rows=build(),
        footer="Enter ejecutar opción · Esc atrás",
        on_enter=on_enter,
        on_activate=activate,
    )


def final_screen(app: App, ctx: Context) -> Screen:
    """Build the closing screen with the hand-off instructions."""
    written = ", ".join(ctx.written) or "los archivos ya existentes"
    rows = [
        Row("heading", label="Configuración lista"),
        Row("note", label=f"Escrito: {written}", result="ok"),
        Row("note", label=f"Dependencias: {ctx.install_result or 'pendientes'}"),
        Row("note", label=""),
        Row("note", label="Siguientes comandos:", result="ok"),
        Row("note", label="  make up        · arrancar las 13 imágenes"),
        Row("note", label="  make status    · comprobar salud"),
        Row("note", label="  make urls      · panel, Temporal y gateway"),
        Row("note", label=""),
        Row("button", label="Validar una última vez", action="validate"),
        Row("button", label="Ver ayuda de teclado", action="help"),
        Row("button", label="Salir", action="quit"),
    ]

    def activate(app_: App, row: Row) -> None:
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=bool(ctx.written)))
        elif row.action == "help":
            app_.info(
                "Ayuda",
                "↑/↓ mover · Enter editar/abrir · Esc atrás",
                "Tab siguiente · a mostrar avanzados",
                "g generar secreto · Ctrl+R valor por defecto",
            )
        elif row.action == "quit":
            app_.should_quit = True

    return Screen(
        title="Listo",
        subtitle=ctx.profile.label,
        rows=rows,
        footer="Enter  ejecutar opción    q  salir",
        on_activate=activate,
    )


def build_app(ctx: Context | None = None) -> App:
    """Create the application with the welcome screen already on the stack.

    Args:
        ctx: Pre-built context, detected on the fly when omitted.

    Returns:
        The configured :class:`App`.
    """
    ctx = ctx or load_context()
    app = App(f"newsvoice · {REPO_ROOT.name}")
    app.push(welcome_screen(app, ctx))
    return app
