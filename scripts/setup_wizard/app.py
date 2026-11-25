"""Screen flow of the guided setup: profile, env files, install, validation.

The screens only build :class:`~curses_ui.Row` lists and react to buttons;
all the data rules live in :mod:`settings`, :mod:`deps` and :mod:`validate`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from engine_runtime import EngineRuntimeError, RuntimePlan

from . import deps, repair, transfer
from . import validate as validation
from .collections import ensure_example_collection
from .config_report import write_config_report
from .curses_ui import App, Dialog, Picker, Row, Screen
from .detect import Report, detect, prerequisites
from .envfile import EnvFile
from .guide import external_guide_screen
from .repair import shared_key_conflicts as _shared_key_conflicts
from .repair import unify_shared_key as _unify_shared_key
from .settings import (
    ENGINE_INFO,
    EXTRA_ENGINE_OPTIONS,
    GPU_ENGINES,
    MODEL_OPTIONS,
    PROFILES,
    ROUTED_PRESETS,
    Field,
    Profile,
    RoutedPreset,
    backend_language_options,
    backend_voice_help,
    backend_voice_options,
    basic_defaults,
    canonical_engine,
    config_requires_gpu,
    configured_engines,
    engine_mode,
    engine_modes,
    engine_option_label,
    engine_remote_urls,
    engine_services,
    engine_terms,
    fields_for,
    gateway_docker_extras,
    gateway_pip_extras,
    join_multi,
    managed_compose_profiles,
    managed_remote_urls,
    parse_engine_map,
    parse_runtime_modes,
    render_engine_map,
    render_runtime_modes,
    required_engine_keys,
    required_model_downloads,
    runtime_env_values,
    runtime_plan,
    runtime_plan_for,
    sections_for,
    selected_engine_infos,
    split_multi,
    valid_language_code,
)
from .util import REPO_ROOT, env_paths, generate_secret

__all__ = ["Context", "build_app", "build_engines_app", "load_context"]

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
        report_path: Secret-free setup report written to disk, when available.
        repairs: Repair descriptions applied during this session.
        manual: Problems that still need operator action.
    """

    report: Report
    envs: dict[str, EnvFile] = field(default_factory=dict)
    profile: Profile = PROFILES[0]
    method: str = "uv"
    written: list[str] = field(default_factory=list)
    install_result: str = ""
    report_path: str = ""
    repairs: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)


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
        "LLM_API_KEY",
        "AI_FIX_API_KEY",
    }
)

# The gateway key is one secret stored in three files: compose injects
# deploy/GATEWAY_API_KEY into the gateway container while news-engine sends
# TTS_API_KEY as X-API-Key. Any divergence turns into 401s at runtime.
_SHARED_KEYS: tuple[tuple[str, str], ...] = (
    ("deploy", "GATEWAY_API_KEY"),
    ("news", "TTS_API_KEY"),
    ("gateway", "API_KEY"),
)
_SHARED_KEY_LABELS = frozenset({"Clave del gateway", "Clave gateway", "API key"})


def _propagate_shared_key(app: App, ctx: Context, value: str) -> None:
    """Keep the three copies of the gateway key equal while the user types."""
    for file_key, key in _SHARED_KEYS:
        if file_key in ctx.envs and ctx.envs[file_key].get(key).strip() != value:
            ctx.envs[file_key].set(key, value)
    for screen in app.screens:
        for row in screen.rows:
            if row.label in _SHARED_KEY_LABELS:
                row.value = value


def _record_repair(ctx: Context, result: repair.RepairResult) -> None:
    """Keep a secret-free summary of staged repairs in the wizard context."""
    for item in result.applied:
        if item not in ctx.repairs:
            ctx.repairs.append(item)
    ctx.manual = result.manual
    if result.error and result.error not in ctx.manual:
        ctx.manual.append(result.error)


def repair_screen(
    app: App,
    ctx: Context,
    *,
    all_problems: bool = False,
    on_done: Callable[[App], None],
) -> Screen:
    """Show staged repairs that can fix engine or configuration problems."""

    def fixes() -> list[repair.Fix]:
        return repair.all_fixes(ctx) if all_problems else repair.conflict_fixes(ctx)

    def build() -> list[Row]:
        current = fixes()
        title = "Todos los problemas" if all_problems else "Conflictos de engines"
        rows = [
            Row("note", label=f"{title}: {len(current)} reparación(es) disponible(s)."),
            Row("note", label="Los cambios quedan en memoria hasta guardar los .env."),
            Row("note", label=""),
        ]
        if not current:
            rows.append(Row("note", label="No hay reparaciones pendientes.", result="ok"))
        for fix in current:
            rows.append(
                Row(
                    "button",
                    label=f"Aplicar: {fix.label}",
                    value=fix.detail,
                    action=f"fix:{fix.id}",
                )
            )
        if current:
            rows.append(Row("button", label="Aplicar todas las visibles", action="fix_all"))
        if ctx.repairs:
            rows.append(Row("note", label=f"Ya aplicadas: {len(ctx.repairs)}", result="ok"))
        if ctx.manual:
            rows.append(
                Row(
                    "note",
                    label=f"Pendiente manual: {len(ctx.manual)} problema(s)",
                    result="warn",
                )
            )
        rows.extend(
            [
                Row("note", label=""),
                Row("button", label="Volver", action="back"),
            ]
        )
        return rows

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            on_done(app_)
            return
        current = fixes()
        if row.action == "fix_all":
            result = repair.apply_fixes(ctx, current)
            _record_repair(ctx, result)
            level = "bad" if result.error else "ok"
            message = result.error or f"{len(result.applied)} reparación(es) aplicada(s)"
            app_.say(message, level)
        elif row.action.startswith("fix:"):
            fix_id = row.action.split(":", 1)[1]
            selected = next((item for item in current if item.id == fix_id), None)
            if selected is None:
                return
            result = repair.apply_fixes(ctx, [selected])
            _record_repair(ctx, result)
            level = "bad" if result.error else "ok"
            message = result.error or (result.applied[0] if result.applied else "Sin cambios")
            app_.say(message, level)
        app_.current.rows = build()
        app_.refocus()

    return Screen(
        title="Reparación de configuración",
        subtitle="Cambios reversibles en memoria · se aplican al guardar",
        rows=build(),
        footer="F1 Ayuda   Enter Ejecutar   Esc Atrás   F10 Salir",
        on_activate=activate,
        on_help=lambda app_: app_.info(
            "Reparación",
            "Los conflictos se resuelven moviendo engines a sidecar/modal o retirándolos.",
            "Solucionar todo también alinea secretos internos, dispositivo y modelos.",
            "Las credenciales externas y los requisitos del host siguen siendo manuales.",
        ),
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
    return fields_for(file_key)


def _apply_basic_config(ctx: Context) -> None:
    """Stage the basic profile: CPU engines, internal secrets, external LLM.

    The endpoint, model and optional API key are collected by the basic screen
    and staged before this helper runs. Missing internal secrets are minted
    here so the gateway and admin panel are not left open.
    """
    docker = next((profile for profile in PROFILES if profile.key == "docker"), PROFILES[0])
    ctx.profile = docker
    if not deps.method_available(ctx.method)[0]:
        ctx.method = "uv" if deps.method_available("uv")[0] else "venv"
    for (file_key, key), value in basic_defaults(docker.key).items():
        env = ctx.envs.get(file_key)
        if env is not None:
            env.set(key, value)
    for file_key, key in (
        ("deploy", "GATEWAY_API_KEY"),
        ("news", "ADMIN_API_TOKEN"),
        ("news", "DOC_TTS_LINK_SECRET"),
    ):
        env = ctx.envs.get(file_key)
        if env is not None and not env.get(key).strip():
            env.set(key, generate_secret())
    _sync_shared_key(ctx)
    _sync_gateway_docker_extras(ctx)
    _sync_engine_requirements(ctx)


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


def _sync_gateway_docker_extras(ctx: Context) -> None:
    """Keep the gateway image extras aligned with the selected engines."""
    deploy = ctx.envs.get("deploy")
    if deploy is None:
        return
    engine, engine_map, extra = _gateway_selection(ctx)
    extras = gateway_docker_extras(engine, engine_map, extra, _gateway_modes(ctx))
    if deploy.get("GATEWAY_UV_EXTRAS").strip() != extras:
        deploy.set("GATEWAY_UV_EXTRAS", extras)


def _sync_engine_requirements(ctx: Context) -> None:
    """Keep Compose profiles, GPU overlay and sidecar URLs aligned with TTS."""
    deploy = ctx.envs.get("deploy")
    if deploy is None:
        return
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = _gateway_modes(ctx)
    try:
        plan = _current_runtime_plan(ctx)
    except EngineRuntimeError:
        plan = None
    if plan is not None:
        _apply_runtime_plan(ctx, plan)

    profiles = managed_compose_profiles(
        deploy.get("COMPOSE_PROFILES"),
        engine,
        engine_map,
        extra,
        plan.profiles if plan is not None else None,
    )
    if deploy.get("COMPOSE_PROFILES").strip() != profiles:
        deploy.set("COMPOSE_PROFILES", profiles)

    gpu = "1" if config_requires_gpu(engine, engine_map, extra, modes) else "0"
    if deploy.get("GATEWAY_GPU").strip() != gpu:
        deploy.set("GATEWAY_GPU", gpu)

    required = engine_remote_urls(engine, engine_map, extra, modes) if plan is None else {}
    for (file_key, key), default in managed_remote_urls().items():
        env = ctx.envs.get(file_key)
        if env is None or plan is not None:
            continue
        current = env.get(key).strip()
        target = required.get((file_key, key), "")
        if target:
            if not current or current == default:
                if current != target:
                    env.set(key, target)
        elif current == default:
            env.set(key, "")


def _changes(ctx: Context) -> dict[str, list[tuple[str, str, str]]]:
    """Return the pending diff of every env file."""
    return {key: ctx.envs[key].diff_against_disk() for key in _ORDER}


def _progress_bar(done: int, total: int, width: int = 18) -> str:
    """Render a compact text progress bar."""
    filled = min(width, round(width * done / total)) if total else 0
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _run_steps_with_progress(
    app: App,
    steps: list[deps.Step],
    *,
    title: str,
    subtitle: str = "",
    followup: list[deps.Step] | None = None,
) -> bool:
    """Run ``steps`` in a modal that lists every action and its status.

    ``followup`` is shown from the start but only runs when the first phase
    succeeds, mirroring the old "install, then verify" sequence.
    """
    followup = followup or []
    actions = [*steps, *followup]
    statuses = {step.title: "pending" for step in actions}
    details = {step.title: step.detail for step in actions}
    symbols = {"pending": " ", "run": "▸", "ok": "✓", "bad": "✗", "skipped": "–"}
    modal = Dialog(title, [], [("Cerrar", lambda app_: app_.close_modal())])
    app.modal = modal

    def render(final_ok: bool | None = None) -> None:
        done = sum(1 for status in statuses.values() if status in {"ok", "bad"})
        total = len(actions)
        percent = round(done * 100 / total) if total else 0
        lines: list[str] = []
        if subtitle:
            lines.append(subtitle)
        lines.append(f"Progreso: {done}/{total} ({percent}%) {_progress_bar(done, total)}")
        lines.append("")
        lines.append("Acciones:")
        for step in actions:
            status = statuses[step.title]
            lines.append(f"[{symbols.get(status, ' ')}] {step.title}")
            if status == "run":
                command = " ".join(str(part) for part in step.command)
                lines.append(f"    $ {command}")
            elif status == "bad":
                lines.append(f"    {details[step.title]}")
            elif status == "skipped":
                lines.append("    omitido: la fase anterior falló")
        if final_ok is not None:
            lines.append("")
            lines.append("Instalación correcta." if final_ok else "Instalación con fallos.")
        modal.lines = lines
        app.redraw()

    def emit(status: str, step_title: str, detail: str) -> None:
        if step_title in statuses:
            statuses[step_title] = status
        if status == "bad":
            details[step_title] = detail
        symbol = symbols.get(status, "·")
        level = {"ok": "ok", "bad": "bad"}.get(status, "")
        app.say(f"{symbol} {step_title}: {detail}"[:200], level)
        render()

    render()
    ok = deps.execute(steps, emit)
    if ok and followup:
        ok = deps.execute(followup, emit)
    elif followup:
        for step in followup:
            statuses[step.title] = "skipped"
    modal.title = f"{title} · {'correcto' if ok else 'con fallos'}"
    render(final_ok=ok)
    return ok


# -- screens ----------------------------------------------------------------


def welcome_screen(app: App, ctx: Context) -> Screen:
    """Build the landing screen with the detected environment."""
    rows: list[Row] = [
        Row(
            "note",
            label="Prepara los tres .env, las dependencias y la validación del despliegue.",
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
            Row("button", label="Guía de valores externos (Fish, HF, Telegram…)", action="guide"),
            Row("button", label="Asistente guiado (.env + dependencias)", action="wizard"),
            Row("button", label="Modificar engines TTS (añadir/quitar)", action="engines"),
            Row("button", label="Modos de carga TTS (embedded/sidecar/modal)", action="modes"),
            Row("button", label="Requisitos del sistema", action="prereqs"),
            Row("button", label="Editar los .env ya configurados", action="edit"),
            Row("button", label="Importar valores desde un archivo .env", action="import"),
            Row("button", label="Exportar la configuración a un archivo", action="export"),
            Row("button", label="Volver a detectar el entorno", action="redetect"),
            Row("button", label="Validar la configuración actual", action="audit"),
            Row("button", label="Solventar problemas con IA…", action="ai_fix"),
            Row("button", label="Ayuda de teclado", action="help"),
            Row("button", label="Salir", action="quit"),
        ]
    )

    def activate(app_: App, row: Row) -> None:
        if row.action == "guide":
            app_.push(external_guide_screen(app_))
        elif row.action == "wizard":
            missing = [item for item in prerequisites(ctx.report) if item.missing]
            if missing:
                app_.say(
                    f"Faltan {len(missing)} requisito(s): revísalos antes de continuar",
                    "warn",
                )
                app_.push(prereq_screen(app_, ctx, return_to="mode"))
            else:
                app_.say("Básica o personalizada")
                app_.push(mode_screen(app_, ctx))
        elif row.action == "engines":
            app_.push(tts_screen(app_, ctx, standalone=True))
        elif row.action == "modes":
            app_.push(runtime_modes_screen(app_, ctx, standalone=True))
        elif row.action == "prereqs":
            app_.push(prereq_screen(app_, ctx, return_to="welcome"))
        elif row.action == "edit":
            app_.push(edit_picker_screen(app_, ctx))
        elif row.action == "import":
            app_.push(import_screen(app_, ctx))
        elif row.action == "export":
            app_.push(export_screen(app_, ctx))
        elif row.action == "redetect":
            ctx.report = detect()
            app_.say("Entorno actualizado", "ok")
            app_.replace(welcome_screen(app_, ctx))
        elif row.action == "audit":
            app_.push(validate_screen(app_, ctx, write_mode=False))
        elif row.action == "ai_fix":
            app_.push(ai_fix_screen(app_, ctx, write_mode=False))
        elif row.action == "help":
            app_.info(
                "Ayuda",
                "↑/↓ o Tab mover · Enter seleccionar/editar",
                "Esc atrás · F10 o q salir",
                "g generar secreto · a mostrar avanzados",
                "Ctrl+R valor por defecto · Espacio sí/no",
            )
        elif row.action == "quit":
            app_.ask_quit()

    return Screen(
        title="Instalador de newsvoice",
        subtitle="Configuración guiada del entorno",
        rows=rows,
        footer="F1 Ayuda   Enter Seleccionar   ↑/↓ Mover   F10 Salir",
        progress="Menú",
        on_activate=activate,
    )


def prereq_screen(app: App, ctx: Context, *, return_to: str = "welcome") -> Screen:
    """Build the prerequisite list, each one with the command that provides it.

    Args:
        app: Running application.
        ctx: Shared wizard state.
        return_to: ``mode`` keeps the guided flow going (basic/custom picker),
            ``profile`` jumps straight to the deployment shape, ``welcome``
            goes back to the menu (the entry point of a standalone inspection).
    """

    def build() -> list[Row]:
        items = prerequisites(ctx.report)
        missing = [item for item in items if item.missing]
        automated = [item for item in items if item.automated]
        hard = any(item.status == "bad" for item in items)
        rows: list[Row] = [
            Row(
                "note",
                label=(
                    f"{len(items) - len(missing)}/{len(items)} listos · "
                    + (f"faltan {len(missing)}" if missing else "nada que instalar")
                ),
                result="bad" if hard else ("warn" if missing else "ok"),
            ),
            Row("note", label=""),
        ]
        for item in items:
            rows.append(
                Row(
                    "note",
                    label=f"{item.symbol} {item.name} — {item.detail}",
                    result=item.status,
                )
            )
            if not item.missing:
                continue
            rows.append(Row("note", label=f"   hace falta porque: {item.why}"))
            if item.remedy:
                rows.extend(Row("note", label=f"   $ {command}") for command in item.remedy)
            else:
                rows.append(Row("note", label="   instalación manual (sin comando seguro)"))
        if any("sudo" in command for item in items for command in item.remedy):
            rows.extend(
                [
                    Row("note", label=""),
                    Row(
                        "note",
                        label="sudo necesita contraseña en tu terminal: si el paso falla, "
                        "copia el comando y ejecútalo a mano.",
                        result="warn",
                    ),
                ]
            )
        rows.append(Row("note", label=""))
        if automated:
            rows.append(Row("button", label="Instalar los que se pueda ahora", action="install"))
        rows.append(Row("button", label="Ver todos los comandos", action="cmds"))
        rows.append(Row("button", label="Volver a detectar", action="redetect"))
        rows.append(
            Row(
                "button",
                label=(
                    "Continuar"
                    if return_to == "mode"
                    else ("Ir al perfil" if return_to == "profile" else "Volver al menú")
                ),
                action="next",
            )
        )
        rows.append(Row("button", label="Atrás", action="back"))
        return rows

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "redetect":
            ctx.report = detect()
            app_.current.rows = build()
            app_.refocus()
            app_.say("Entorno actualizado", "ok")
            return
        if row.action == "cmds":
            lines = []
            for item in prerequisites(ctx.report):
                lines.append(f"{item.name}: " + (" · ".join(item.remedy) or "manual"))
            app_.info("Instalación de requisitos", *lines)
            return
        if row.action == "next":
            if return_to == "mode":
                app_.push(mode_screen(app_, ctx))
            elif return_to == "profile":
                app_.push(profile_screen(app_, ctx))
            else:
                app_.pop()
            return
        if row.action != "install":
            return
        steps = deps.prereq_steps(prerequisites(ctx.report))
        if not steps:
            app_.say("Nada se puede instalar automáticamente desde aquí")
            return
        ok = _run_steps_with_progress(
            app_,
            steps,
            title="Instalando requisitos",
            subtitle=f"Se ejecutarán {len(steps)} acción(es).",
        )
        ctx.report = detect()
        app_.say(
            "Requisitos instalados" if ok else "Algunos comandos han fallado: cópialos a mano",
            "ok" if ok else "warn",
        )
        app_.current.rows = build()
        app_.refocus()

    return Screen(
        title="Paso 0 · requisitos",
        subtitle="Qué falta y cómo instalarlo",
        rows=build(),
        footer="F1 Ayuda   Enter Ejecutar   Esc Atrás   F10 Salir",
        progress="0/6",
        on_enter=on_enter,
        on_activate=activate,
    )


def mode_screen(app: App, ctx: Context) -> Screen:
    """Build the basic vs. custom configuration picker."""
    rows = [
        Row("note", label="Elige el nivel de configuración del asistente."),
        Row("note", label=""),
        Row(
            "button",
            label="Básica (recomendada) · CPU, todos los engines TTS locales, LLM externo",
            action="basic",
        ),
        Row(
            "button",
            label="Personalizada · perfil, método, engines, puertos y credenciales",
            action="custom",
        ),
        Row("note", label=""),
        Row("button", label="Atrás", action="back"),
    ]

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "basic":
            app_.say("Configuración básica: revisa URL, modelo y clave del LLM")
            app_.push(basic_screen(app_, ctx))
            return
        if row.action == "custom":
            app_.say("Elige el punto de partida")
            app_.push(profile_screen(app_, ctx))

    return Screen(
        title="Paso 1 · tipo de configuración",
        subtitle="Básica o personalizada",
        rows=rows,
        footer="F1 Ayuda   Enter Seleccionar   ↑/↓ Mover   Esc Atrás   F10 Salir",
        progress="1/6",
        on_activate=activate,
    )


def basic_screen(app: App, ctx: Context) -> Screen:
    """Build the basic setup summary plus the external LLM connection."""
    news = ctx.envs["news"]
    llm_fields = {field.key: field for field in _fields("news")}
    llm_base = Row(
        "text",
        label="URL LLM",
        value=_value_for(ctx, llm_fields["LLM_API_BASE"]),
        help="Endpoint OpenAI-compatible. En Docker usa host.docker.internal, no localhost.",
        action="llm_base",
    )
    llm_model = Row(
        "text",
        label="Modelo LLM",
        value=_value_for(ctx, llm_fields["LLM_MODEL"]),
        help="Id exacto del modelo servido por la URL anterior (/v1/models).",
        action="llm_model",
    )
    llm_key = Row(
        "secret",
        label="Clave API LLM (opcional)",
        value=news.get("LLM_API_KEY"),
        help="Vacía con llama.cpp local; rellénala si tu proveedor exige autenticación.",
        action="llm_key",
    )
    rows = [
        Row(
            "note",
            label="Básica: Docker Shape A · CPU · todos los engines TTS locales.",
            result="ok",
        ),
        Row("note", label="Engines: pocket_tts (es/en/fr), piper (ca), kokoro y edgetts extra."),
        Row("note", label="TTS_DEVICE=cpu · sin sidecars GPU · se generan las claves internas."),
        Row("note", label="LLM externo: configura la URL y el modelo; la clave es opcional."),
        Row("note", label=""),
        llm_base,
        llm_model,
        Row("note", label=""),
        llm_key,
        Row("note", label=""),
        Row("button", label="Aplicar configuración básica", action="apply"),
        Row("button", label="Atrás", action="back"),
    ]

    def on_change(app_: App, row: Row) -> None:
        keys = {
            "llm_base": "LLM_API_BASE",
            "llm_model": "LLM_MODEL",
            "llm_key": "LLM_API_KEY",
        }
        key = keys.get(row.action)
        if key is not None:
            news.set(key, row.value.strip())

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "apply":
            news.set("LLM_API_BASE", llm_base.value.strip())
            news.set("LLM_MODEL", llm_model.value.strip())
            news.set("LLM_API_KEY", llm_key.value.strip())
            _apply_basic_config(ctx)
            app_.say("Configuración básica aplicada: confirma los términos", "ok")
            app_.push(basic_terms_screen(app_, ctx))

    return Screen(
        title="Paso 2 · configuración básica",
        subtitle="Valores CPU y conexión del LLM externo",
        rows=rows,
        footer="F1 Ayuda   Enter Editar   Esc Atrás   F10 Salir",
        progress="2/6",
        on_change=on_change,
        on_activate=activate,
    )


def basic_terms_screen(app: App, ctx: Context) -> Screen:
    """Ask explicitly for the licenses of the engines staged by the basic mode."""

    def build() -> list[Row]:
        engine, engine_map, extra = _gateway_selection(ctx)
        modes = _gateway_modes(ctx)
        accepted = _accepted_terms(ctx)
        rows: list[Row] = [
            Row("note", label="El perfil básico selecciona engines con licencia propia."),
            Row("note", label="Confirma que has leído sus términos antes de continuar."),
            Row("note", label=""),
        ]
        terms = engine_terms(engine, engine_map, extra, modes)
        if not terms:
            rows.append(Row("note", label="La selección no exige aceptación explícita."))
        for info in terms:
            rows.append(
                Row(
                    "bool",
                    label=f"Acepto los términos de {info.label}",
                    value="true" if info.key in accepted else "false",
                    help=info.terms_url or "Sin URL de términos declarada.",
                    action=f"terms:{info.key}",
                )
            )
            if info.terms_url:
                rows.append(Row("note", label=f"  {info.terms_url}"))
        rows.append(Row("note", label=""))
        rows.append(Row("button", label="Continuar", action="continue"))
        rows.append(Row("button", label="Atrás", action="back"))
        return rows

    def on_change(app_: App, row: Row) -> None:
        if not row.action.startswith("terms:"):
            return
        key = row.action.split(":", 1)[1]
        accepted = _accepted_terms(ctx)
        if row.value.strip().lower() in ("true", "1", "yes"):
            if key not in accepted:
                accepted.append(key)
        else:
            accepted = [name for name in accepted if name != key]
        _store_accepted_terms(ctx, accepted)

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "continue":
            engine, engine_map, extra = _gateway_selection(ctx)
            pending = [
                info.label
                for info in engine_terms(engine, engine_map, extra, _gateway_modes(ctx))
                if info.key not in _accepted_terms(ctx)
            ]
            if pending:
                app_.say("Faltan términos por aceptar: " + ", ".join(pending), "warn")
                return
            app_.push(review_screen(app_, ctx))

    return Screen(
        title="Paso 2b · términos y licencias",
        subtitle="Engines seleccionados por el perfil básico",
        rows=build(),
        footer="F1 Ayuda   Espacio Alternar   Enter Continuar   Esc Atrás   F10 Salir",
        progress="2/6",
        on_change=on_change,
        on_activate=activate,
    )


def profile_screen(app: App, ctx: Context) -> Screen:
    """Build the deployment shape picker."""
    labels = [profile.label for profile in PROFILES]
    by_label = {profile.label: profile for profile in PROFILES}
    ctx.profile = by_label.get(ctx.profile.label, PROFILES[0])

    desc = Row("note", label=ctx.profile.description, result="ok")
    rows = [
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
        subtitle="Punto de partida de la configuración",
        rows=rows,
        footer="F1 Ayuda   ←/→ Cambiar   Enter Continuar   Esc Atrás   F10 Salir",
        progress="1/6",
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
        subtitle="Cómo instalar el entorno del proyecto",
        rows=rows,
        footer="F1 Ayuda   ←/→ Cambiar   Enter Continuar   Esc Atrás   F10 Salir",
        progress="2/6",
        on_change=on_change,
        on_activate=activate,
    )


def _field_status(field_: Field) -> str:
    """Return the compact legend for one field."""
    flags = [field_.source, "obligatorio" if field_.required else "opcional"]
    if field_.advanced:
        flags.append("avanzado")
    if field_.kind == "choice":
        flags.append("elección")
    if field_.kind == "password":
        flags.append("hash bcrypt")
    if field_.generate:
        flags.append("g genera")
    return " · ".join(flags)


def _show_env_help(app: App, ctx: Context, file_key: str) -> None:
    """Show the detailed field reference for one env file."""
    env = ctx.envs[file_key]
    lines = [
        f"Archivo: {env.key_path}",
        "PgUp/PgDn desplazan · F1/Esc cierran",
        "",
        "Cada campo indica origen (interno/externo), obligatoriedad, valor efectivo y opciones.",
        "",
    ]
    for section in sections_for(file_key):
        lines.append(f"── {section.title} ──")
        for field_ in section.fields:
            current = env.get(field_.key).strip()
            shown = (
                _mask(current) if field_.kind in ("secret", "password") else (current or "(vacío)")
            )
            lines.append(f"{field_.label} ({field_.key})")
            lines.append(f"  {field_.detail}")
            if field_.detail != field_.help:
                lines.append(f"  Resumen: {field_.help}")
            lines.append(f"  {_field_status(field_)} · actual: {shown}")
            if field_.default:
                lines.append(f"  por defecto: {field_.default}")
            if field_.options:
                lines.append("  opciones: " + ", ".join(field_.options))
            lines.append("")
    app.info(f"Ayuda · {file_key}/.env", *lines)


def env_screen(app: App, ctx: Context, file_key: str, *, edit_mode: bool = False) -> Screen:
    """Build the form for one env file, sections included.

    Args:
        app: Running application.
        ctx: Shared wizard state.
        file_key: One of ``deploy``, ``news`` or ``gateway``.
        edit_mode: Swap the wizard continuation buttons for a direct
            «guardar en disco» action (used by the edit/import screens).
    """
    from .settings import sections_for as _sections

    env = ctx.envs[file_key]
    target = next(t for t in env_paths() if t.key == file_key)
    gpu_warning = Row("note", label="", result="")
    tts_hint = Row("note", label="", result="")
    rows: list[Row] = [
        Row(
            "note",
            label=f"{target.key_path} — "
            + ("se creará desde su plantilla" if not env.exists else "ya existe"),
            result="ok" if env.exists else "warn",
        ),
        Row("note", label=""),
        Row(
            "note",
            label="[interno] lo aporta el stack · [externo] requiere proveedor o API externa",
        ),
        gpu_warning,
        Row("note", label=""),
    ]
    field_rows: dict[str, Row] = {}
    for section in _sections(file_key):
        rows.append(Row("heading", label=section.title))
        for field_ in section.fields:
            value = _value_for(ctx, field_)
            row = Row(
                kind="password" if field_.kind == "password" else field_.kind,
                label=field_.label,
                value=value,
                help=field_.help,
                options=list(field_.options),
                default=field_.default,
                generate=field_.generate,
                advanced=field_.advanced,
                secret=field_.kind in ("secret", "password"),
                source=field_.source,
                action=field_.key,
            )
            field_rows[field_.key] = row
            rows.append(row)
        if file_key == "news" and section.title == "TTS y gateway":
            rows.append(tts_hint)
        rows.append(Row("note", label=""))
    if file_key == "gateway":
        rows.append(
            Row(
                "button",
                label="Asistente TTS guiado (engines, routed y modelos)",
                action="tts",
            )
        )
    if edit_mode:
        rows.extend(
            [
                Row("button", label="Ver cambios de este archivo", action="diff"),
                Row("button", label="Guardar en disco", action="save"),
                Row("button", label="Volver al menú de edición", action="back"),
            ]
        )
    else:
        rows.extend(
            [
                Row("button", label="Ver cambios de este archivo", action="diff"),
                Row("button", label="Guardar en disco y continuar", action="next"),
                Row("button", label="Atrás", action="back"),
            ]
        )
    if file_key == "gateway":
        _apply_gpu_lock(app, ctx, field_rows, gpu_warning)
        _sync_required_models(ctx, field_rows)
    if file_key == "news":
        _sync_news_tts_options(ctx, field_rows, tts_hint)

    def on_change(app_: App, row: Row) -> None:
        field_ = _find_field(file_key, row)
        if field_ is None:
            return
        shared = (file_key, field_.key) in _SHARED_KEYS
        value = row.value.strip() if shared else row.value
        ctx.envs[file_key].set(field_.key, value)
        if shared:
            row.value = value
            _propagate_shared_key(app_, ctx, value)
            app_.say("Clave del gateway sincronizada en los tres archivos", "ok")
        if file_key == "gateway" and field_.key.startswith("TTS_"):
            _apply_gpu_lock(app_, ctx, field_rows, gpu_warning)
            _sync_required_models(ctx, field_rows)
            _sync_gateway_docker_extras(ctx)
            _sync_engine_requirements(ctx)
        if file_key == "news" and field_.key == "TTS_DEFAULT_BACKEND":
            _sync_news_tts_options(ctx, field_rows, tts_hint)

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "tts":
            app_.push(tts_screen(app_, ctx, edit_mode=edit_mode))
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
        if row.action == "save":
            _sync_gateway_docker_extras(ctx)
            _sync_engine_requirements(ctx)
            if not env.changed_keys():
                app_.say("Sin cambios que escribir en este archivo", "")
                return
            app_.modal = _confirm_write_one(app_, ctx, file_key)
            return
        if row.action == "next":
            _sync_shared_key(ctx)
            _sync_gateway_docker_extras(ctx)
            _sync_engine_requirements(ctx)
            if env.changed_keys():
                ok, _backup, _names = _write_env(app_, env, reload=True)
                if not ok:
                    return
                if env.key_path not in ctx.written:
                    ctx.written.append(env.key_path)
            position = _ORDER.index(file_key) if file_key in _ORDER else -1
            nxt = _ORDER[position + 1] if 0 <= position < len(_ORDER) - 1 else None
            if nxt:
                app_.push(env_screen(app_, ctx, nxt))
            else:
                app_.push(review_screen(app_, ctx))

    return Screen(
        title=f"{'Editar' if edit_mode else 'Paso 3'} · {file_key}",
        subtitle=target.label,
        rows=rows,
        footer="F1 Ayuda   Enter Editar   Tab Siguiente   a Avanzados   Esc Atrás   F10 Salir",
        progress="" if edit_mode else "3/6",
        on_change=on_change,
        on_activate=activate,
        on_help=lambda app_: _show_env_help(app_, ctx, file_key),
    )


def _find_field(file_key: str, row: Row) -> Field | None:
    for field_ in _fields(file_key):
        if field_.label == row.label:
            return field_
    return None


def _gateway_selection(ctx: Context) -> tuple[str, str, str]:
    """Return the gateway engine selection used to derive voices and models."""
    gateway = ctx.envs.get("gateway")
    if gateway is None:
        return "", "", ""
    return (
        gateway.get("TTS_ENGINE").strip(),
        gateway.get("TTS_ENGINES").strip(),
        gateway.get("TTS_EXTRA_ENGINES").strip(),
    )


def _gateway_modes(ctx: Context) -> dict[str, str]:
    """Return the runtime mode overrides stored in gateway/.env."""
    gateway = ctx.envs.get("gateway")
    return parse_runtime_modes(gateway.get("TTS_RUNTIME_MODES")) if gateway else {}


def _current_runtime_plan(
    ctx: Context,
    overrides: dict[str, str] | None = None,
) -> RuntimePlan:
    """Resolve the staged gateway selection through the canonical planner."""
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = {**_gateway_modes(ctx), **(overrides or {})}
    return runtime_plan_for(engine, engine_map, extra, modes)


def _apply_runtime_plan(ctx: Context, plan: RuntimePlan) -> None:
    """Stage runtime modes, profiles and sidecar URLs in every env file."""
    derived = runtime_env_values(plan)
    modes = derived.get("TTS_RUNTIME_MODES", render_runtime_modes({}))
    profiles = derived.get("TTS_RUNTIME_PROFILES", "")
    for env in ctx.envs.values():
        if env.has("TTS_RUNTIME_MODES") and env.get("TTS_RUNTIME_MODES").strip() != modes:
            env.set("TTS_RUNTIME_MODES", modes)
        if env.has("TTS_RUNTIME_PROFILES") and env.get("TTS_RUNTIME_PROFILES").strip() != profiles:
            env.set("TTS_RUNTIME_PROFILES", profiles)

    urls = {key: value for key, value in plan.urls.items()}
    for (file_key, key), default in managed_remote_urls().items():
        env = ctx.envs.get(file_key)
        if env is None:
            continue
        current = env.get(key).strip()
        target = urls.get(key, "")
        if target:
            if not current or current == default:
                if current != target:
                    env.set(key, target)
        elif current == default:
            env.set(key, "")


def _sync_news_tts_options(
    ctx: Context,
    rows_by_key: dict[str, Row],
    hint: Row,
) -> None:
    """Update news language and voice choices from the selected backend.

    The news app sends Edge ShortNames directly to Edge but gateway stock ids
    for every other backend. Engines without stock voices switch the voice row
    back to text and the hint explains how to provide a clone or provider id.
    """
    news = ctx.envs.get("news")
    if news is None:
        return
    backend_row = rows_by_key.get("TTS_DEFAULT_BACKEND")
    language_row = rows_by_key.get("TTS_LANGUAGE")
    voice_row = rows_by_key.get("TTS_VOICE")
    if backend_row is None or language_row is None or voice_row is None:
        return
    backend = backend_row.value.strip() or "edge"
    gateway_engine, engine_map, extra = _gateway_selection(ctx)
    languages = backend_language_options(backend, gateway_engine, engine_map, extra)
    voices = backend_voice_options(backend, gateway_engine, engine_map, extra)

    if languages:
        language_row.kind = "choice"
        language_row.options = languages
        language_row.help = (
            f"Idiomas de {backend}; también fija el idioma del digest. "
            "Cambiar de engine actualiza esta lista."
        )
        preferred = "es" if "es" in languages else languages[0]
        language_row.default = preferred
        if language_row.value not in languages:
            language_row.value = preferred
            news.set("TTS_LANGUAGE", preferred)
    else:
        language_row.kind = "text"
        language_row.options = []
        language_row.default = ""
        language_row.help = "El engine no declara idiomas por defecto; escribe un código ISO 639-1."

    voice_help = backend_voice_help(backend, gateway_engine, engine_map, extra)
    if voices:
        voice_row.kind = "choice"
        voice_row.options = voices
        voice_row.help = voice_help
        voice_row.default = voices[0]
        if voice_row.value not in voices:
            voice_row.value = voices[0]
            news.set("TTS_VOICE", voices[0])
    else:
        voice_row.kind = "text"
        voice_row.options = []
        voice_row.default = ""
        voice_row.help = voice_help

    if voices:
        hint.label = f"{backend}: {len(languages)} idioma(s) · {len(voices)} voces stock."
        hint.result = "ok"
    else:
        hint.label = voice_help
        hint.result = "warn"


def _sync_required_models(ctx: Context, rows_by_key: dict[str, Row]) -> None:
    """Add and lock the checkpoints required by the selected gateway engines."""
    gateway = ctx.envs.get("gateway")
    models_row = rows_by_key.get("TTS_MODEL_DOWNLOADS")
    if gateway is None or models_row is None:
        return
    gateway_engine, engine_map, extra = _gateway_selection(ctx)
    required = required_model_downloads(gateway_engine, engine_map, extra, _gateway_modes(ctx))
    models_row.required_options = required
    current = split_multi(models_row.value)
    missing = [model for model in required if model not in current]
    if missing:
        current.extend(missing)
        value = join_multi(current)
        models_row.value = value
        gateway.set("TTS_MODEL_DOWNLOADS", value)
    if required:
        models_row.help = (
            "Obligatorios para el engine seleccionado: "
            + ", ".join(required)
            + ". Marca otros para precargar; los cloud no descargan pesos."
        )
    else:
        models_row.help = (
            "El engine seleccionado no usa pesos locales; puedes precargar otros checkpoints."
        )


def _apply_gpu_lock(
    app: App,
    ctx: Context,
    rows_by_key: dict[str, Row],
    warning: Row,
) -> None:
    """Force ``cuda:0`` while the selection includes a GPU engine."""
    gateway = ctx.envs.get("gateway")
    if gateway is None:
        return
    required = config_requires_gpu(
        gateway.get("TTS_ENGINE"),
        gateway.get("TTS_ENGINES"),
        gateway.get("TTS_EXTRA_ENGINES"),
        _gateway_modes(ctx),
    )
    device = rows_by_key.get("TTS_DEVICE")
    if device is not None:
        if required:
            device.options = ["cuda:0"]
            device.help = "Bloqueado en cuda:0: la selección incluye engines GPU."
            if device.value != "cuda:0":
                device.value = "cuda:0"
                gateway.set("TTS_DEVICE", "cuda:0")
                app.say("GPU obligatoria: TTS_DEVICE corregido a cuda:0", "warn")
        else:
            if "cpu" not in device.options:
                device.options = ["cuda:0", "cpu"]
            device.help = "cuda:0 exige el overlay GPU; cpu funciona sin gráfica."
    if required:
        gpu = [
            name
            for name in configured_engines(
                gateway.get("TTS_ENGINE"),
                gateway.get("TTS_ENGINES"),
                gateway.get("TTS_EXTRA_ENGINES"),
            )
            if name in GPU_ENGINES
        ]
        warning.label = (
            "⚠ La selección incluye engines GPU ("
            + ", ".join(gpu)
            + "): TTS_DEVICE queda fijado en cuda:0."
        )
        warning.result = "warn"
    else:
        warning.label = ""
        warning.result = ""


def _apply_routed_preset(ctx: Context, preset: RoutedPreset) -> None:
    gateway = ctx.envs["gateway"]
    gateway.set("TTS_ENGINE", "routed")
    gateway.set("TTS_ENGINES", preset.engines)
    gateway.set("TTS_EXTRA_ENGINES", preset.extra_engines)
    gateway.set("TTS_DEVICE", preset.device)
    gateway.set("TTS_MODEL_DOWNLOADS", preset.model_downloads)
    _sync_gateway_docker_extras(ctx)
    _sync_engine_requirements(ctx)


def _open_tts_presets(app: App, ctx: Context, *, edit_mode: bool, standalone: bool = False) -> None:
    by_label = {preset.label: preset for preset in ROUTED_PRESETS}
    labels = list(by_label)

    def pick(app_: App, label: str) -> None:
        preset = by_label[label]
        app_.close_modal()
        _apply_routed_preset(ctx, preset)
        app_.replace(tts_screen(app_, ctx, edit_mode=edit_mode, standalone=standalone))
        app_.say(f"Preset aplicado: {preset.label}", "ok")

    app.modal = Picker(
        "Presets de routed",
        labels,
        labels[0],
        pick,
        "Enter aplica el preset · Esc cancela",
    )


def _back_to_gateway(app: App, ctx: Context, *, edit_mode: bool) -> None:
    app.pop()
    app.replace(env_screen(app, ctx, "gateway", edit_mode=edit_mode))


def _back_to_tts(app: App, ctx: Context, *, edit_mode: bool, standalone: bool = False) -> None:
    app.pop()
    app.replace(tts_screen(app, ctx, edit_mode=edit_mode, standalone=standalone))


def _tts_catalog_lines() -> list[str]:
    lines: list[str] = []
    for info in ENGINE_INFO.values():
        cloning = "clonado sí" if info.cloning else "sin clonado"
        languages = "todos" if info.languages == ("*",) else ", ".join(info.languages)
        voices = f"{len(info.voices)} voces stock" if info.voices else "sin voces stock"
        modes = "/".join(info.modes)
        lines.append(
            f"{info.key}: {info.device} · {modes} · {cloning} · idiomas: {languages} · {voices}"
        )
        lines.append(f"  {info.description}")
    return lines


def _tts_help_lines() -> list[str]:
    return [
        "TTS_ENGINE fija un engine para todo; routed reparte por idioma.",
        "TTS_ENGINES es el mapa idioma→engine; _default es el fallback.",
        "TTS_EXTRA_ENGINES carga engines para pins explícitos, sin rutear idioma.",
        "Los engines GPU (omnivoice, omnivoice_remote, qwen3_tts, chatterbox) "
        "obligan a TTS_DEVICE=cuda:0.",
        "edgetts, fish_tts y openai_tts son cloud: no descargan pesos locales.",
        "El preset CPU seguro usa pocket_tts/piper/kokoro y no requiere GPU.",
        "TTS_MODEL_DOWNLOADS precarga los checkpoints del engine elegido; "
        "el asistente los marca y bloquea.",
        "GATEWAY_UV_EXTRAS se deriva de los engines y añade sus extras pip a la "
        "imagen Docker; reconstruye con make up.",
        "Los engines sin voces stock necesitan una voz clonada o un proveedor.",
        "Quitar un engine de Engines disponibles retira su perfil/URL y su extra "
        "pip de la imagen; reconstruye con make up.",
        "Los presets y el editor de mapa modifican el gateway/.env en memoria; "
        "guarda desde la pantalla de gateway.",
    ]


def _selected_engine_requirements(ctx: Context) -> tuple[list[str], list[str], bool]:
    """Return profiles, services and whether the selection needs a GPU."""
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = _gateway_modes(ctx)
    profiles: list[str] = []
    for info in selected_engine_infos(engine, engine_map, extra, modes):
        profiles.extend(info.profiles)
    services = engine_services(engine, engine_map, extra, modes)
    return (
        list(dict.fromkeys(profiles)),
        services,
        config_requires_gpu(engine, engine_map, extra, modes),
    )


def _requirement_summary(ctx: Context) -> str:
    """Render the one-line engine infrastructure summary used by the TTS screen."""
    profiles, services, gpu = _selected_engine_requirements(ctx)
    parts = [f"GPU {'sí' if gpu else 'no'}"]
    if services:
        parts.append("contenedores: " + ", ".join(services))
    if profiles:
        parts.append("perfiles: " + ", ".join(profiles))
    if not services:
        parts.append("sin sidecars")
    return " · ".join(parts)


def _accepted_terms(ctx: Context) -> list[str]:
    """Return the engine terms explicitly accepted by the operator."""
    deploy = ctx.envs.get("deploy")
    return split_multi(deploy.get("ENGINE_TERMS_ACCEPTED")) if deploy else []


def _store_accepted_terms(ctx: Context, accepted: list[str]) -> None:
    """Persist the accepted terms list in deploy/.env."""
    deploy = ctx.envs.get("deploy")
    if deploy is not None:
        deploy.set("ENGINE_TERMS_ACCEPTED", join_multi(accepted))


def _gateway_field(key: str) -> Field | None:
    """Return the gateway field definition for ``key`` when it exists."""
    return next((field_ for field_ in fields_for("gateway") if field_.key == key), None)


def _engine_credential_rows(ctx: Context) -> list[Row]:
    """Build rows for the credentials relevant to the selected engines."""
    gateway = ctx.envs["gateway"]
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = _gateway_modes(ctx)
    keys: list[str] = []
    for info in selected_engine_infos(engine, engine_map, extra, modes):
        keys.extend(info.required_keys)
        keys.extend(info.optional_keys)
    rows: list[Row] = []
    for key in list(dict.fromkeys(keys)):
        field_ = _gateway_field(key)
        if field_ is None:
            continue
        required = key in required_engine_keys(engine, engine_map, extra, modes)
        value = _value_for(ctx, field_)
        present = bool(gateway.get(key).strip())
        status = "obligatoria y pendiente" if required and not present else ""
        rows.append(
            Row(
                kind="password" if field_.kind == "password" else field_.kind,
                label=field_.label + (" (obligatoria)" if required else " (opcional)"),
                value=value,
                help=field_.help,
                options=list(field_.options),
                default=field_.default,
                generate=field_.generate,
                secret=field_.kind in ("secret", "password"),
                source=field_.source,
                action=f"env:{key}",
                result="warn" if status else ("ok" if present else ""),
            )
        )
        if status:
            rows.append(Row("note", label=f"  {field_.key}: {status}", result="warn"))
    if not rows:
        rows.append(Row("note", label="La selección actual no necesita credenciales de proveedor."))
    return rows


def _set_engine_mode(
    ctx: Context,
    engine: str,
    mode: str,
    *,
    app: App | None = None,
    row: Row | None = None,
) -> bool:
    """Stage one mode change, rolling back when the runtime plan rejects it."""
    gateway = ctx.envs.get("gateway")
    if gateway is None or engine not in ENGINE_INFO or mode not in engine_modes(engine):
        return False
    key = canonical_engine(engine)
    modes = _gateway_modes(ctx)
    snapshot = {
        "TTS_ENGINE": gateway.get("TTS_ENGINE"),
        "TTS_ENGINES": gateway.get("TTS_ENGINES"),
        "TTS_EXTRA_ENGINES": gateway.get("TTS_EXTRA_ENGINES"),
        "TTS_RUNTIME_MODES": gateway.get("TTS_RUNTIME_MODES"),
    }
    previous_mode = modes.get(key, engine_mode(key))
    modes[key] = mode
    engine_name, engine_map, extra = _gateway_selection(ctx)
    selected = configured_engines(engine_name, engine_map, extra)
    extras = split_multi(extra)
    routing = parse_engine_map(engine_map) or {}
    if mode == "disabled":
        selected = [name for name in selected if canonical_engine(name) != key]
        extras = [name for name in extras if canonical_engine(name) != key]
        routing = {
            language: target
            for language, target in routing.items()
            if canonical_engine(target) != key
        }
        if canonical_engine(engine_name) == key:
            engine_name = "routed" if routing or extras else ""
    elif key not in {canonical_engine(name) for name in selected}:
        extras.append(key)
        selected.append(key)

    try:
        plan = runtime_plan(selected, modes)
    except EngineRuntimeError as exc:
        for name, value in snapshot.items():
            gateway.set(name, value)
        if row is not None:
            row.value = previous_mode
        if app is not None:
            app.say(str(exc), "bad")
        return False
    if plan.conflicts:
        for name, value in snapshot.items():
            gateway.set(name, value)
        if row is not None:
            row.value = previous_mode
        if app is not None:
            app.say("No se puede cambiar el modo: " + " · ".join(plan.conflicts), "bad")
        return False

    gateway.set("TTS_ENGINE", engine_name)
    gateway.set("TTS_ENGINES", render_engine_map(routing))
    gateway.set("TTS_EXTRA_ENGINES", join_multi(extras))
    gateway.set("TTS_RUNTIME_MODES", render_runtime_modes(modes))
    _apply_runtime_plan(ctx, plan)
    _sync_gateway_docker_extras(ctx)
    _sync_engine_requirements(ctx)
    if row is not None:
        row.value = mode
    if app is not None:
        label = "desactivado" if mode == "disabled" else mode
        app.say(f"Modo de {key}: {label} (sin escribir en disco)", "ok")
    return True


def runtime_modes_screen(app: App, ctx: Context, *, standalone: bool = False) -> Screen:
    """Edit the load mode of every engine without writing TOML or env files."""

    def build() -> list[Row]:
        modes = _gateway_modes(ctx)
        engine_name, engine_map, extra = _gateway_selection(ctx)
        active = {
            canonical_engine(name) for name in configured_engines(engine_name, engine_map, extra)
        }
        candidates = [engine_name, *EXTRA_ENGINE_OPTIONS, *ENGINE_INFO]
        rows: list[Row] = [
            Row("note", label="Elige dónde carga cada engine; los cambios quedan en memoria."),
            Row(
                "note", label="disabled lo retira de la selección; embed/sidecar/modal se validan."
            ),
            Row("note", label=""),
        ]
        for candidate in dict.fromkeys(candidates):
            key = canonical_engine(candidate)
            info = ENGINE_INFO.get(key)
            if info is None or not info.selectable:
                continue
            current = modes.get(key, engine_mode(key))
            rows.append(
                Row(
                    "choice",
                    label=f"{key} ({'activo' if key in active else 'inactivo'})",
                    value=current,
                    options=list(engine_modes(key)),
                    help=f"Modo de carga de {key}. Combinaciones incompatibles se rechazan.",
                    action=f"mode:{key}",
                )
            )
            if info.modal_function:
                rows.append(Row("note", label=f"  Modal: {info.modal_function}"))
        rows.append(Row("note", label=""))
        rows.append(Row("button", label="Resolver conflictos de engines…", action="fix_conflicts"))
        if standalone:
            rows.append(Row("button", label="Guardar engines", action="save_engines"))
        rows.append(Row("button", label="Volver", action="back"))
        return rows

    def on_change(app_: App, row: Row) -> None:
        if not row.action.startswith("mode:"):
            return
        key = row.action.split(":", 1)[1]
        _set_engine_mode(ctx, key, row.value.strip().lower(), app=app_, row=row)
        app_.current.rows = build()
        app_.refocus()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "fix_conflicts":
            app_.push(
                repair_screen(
                    app_,
                    ctx,
                    on_done=lambda active: active.replace(
                        runtime_modes_screen(active, ctx, standalone=standalone)
                    ),
                )
            )
            return
        if row.action == "save_engines":
            app_.modal = _confirm_engine_write(app_, ctx)

    return Screen(
        title="Modos de carga TTS",
        subtitle="embedded · sidecar · modal · disabled",
        rows=build(),
        footer="F1 Ayuda   ←/→ Cambiar   Enter Confirmar   Esc Atrás   F10 Salir",
        on_change=on_change,
        on_activate=activate,
        on_help=lambda app_: app_.info(
            "Modos de carga",
            "embedded: el engine se instancia dentro del gateway.",
            "sidecar: contenedor propio con perfil Compose y URL dedicada.",
            "modal: función remota; no añade extras pip ni perfiles sidecar.",
            "disabled: retira el engine de la selección.",
            "Los cambios se guardan solo en los .env cuando confirmes el guardado.",
        ),
    )


def engine_requirements_screen(
    app: App,
    ctx: Context,
    *,
    edit_mode: bool = False,
    standalone: bool = False,
) -> Screen:
    """Show containers, GPU, credentials and terms for the selected engines."""
    gateway = ctx.envs["gateway"]
    deploy = ctx.envs["deploy"]

    def build() -> list[Row]:
        engine, engine_map, extra = _gateway_selection(ctx)
        modes = _gateway_modes(ctx)
        infos = selected_engine_infos(engine, engine_map, extra, modes)
        profiles, services, gpu = _selected_engine_requirements(ctx)
        accepted = _accepted_terms(ctx)
        rows: list[Row] = [
            Row("note", label="Requisitos derivados de la selección actual:"),
            Row("note", label=f"  GPU: {'sí' if gpu else 'no'} · " + _requirement_summary(ctx)),
        ]
        if not infos:
            rows.append(Row("note", label="No hay engines seleccionados.", result="warn"))
        for info in infos:
            scope = {"gateway": "gateway principal", "sidecar": "sidecar GPU"}.get(
                info.gpu_scope, "sin GPU"
            )
            rows.append(
                Row(
                    "note",
                    label=f"  {info.label}: {info.device} · modo: "
                    + modes.get(canonical_engine(info.key), info.mode)
                    + f" · {scope}"
                    + (f" · perfiles: {', '.join(info.profiles)}" if info.profiles else "")
                    + (f" · servicios: {', '.join(info.services)}" if info.services else ""),
                )
            )
            if not info.docker_supported:
                rows.append(
                    Row(
                        "note",
                        label="    Docker no incluye este engine; " + info.manual_install,
                        result="warn",
                    )
                )
        rows.append(Row("note", label=""))
        rows.append(Row("heading", label="Credenciales y API"))
        rows.extend(_engine_credential_rows(ctx))
        rows.append(Row("note", label=""))
        rows.append(Row("heading", label="Términos y licencias"))
        terms = engine_terms(engine, engine_map, extra, modes)
        if not terms:
            rows.append(Row("note", label="La selección no exige aceptación explícita."))
        for info in terms:
            rows.append(
                Row(
                    "bool",
                    label=f"Acepto los términos de {info.label}",
                    value="true" if info.key in accepted else "false",
                    help=info.terms_url or "Sin URL de términos declarada.",
                    action=f"terms:{info.key}",
                )
            )
            if info.terms_url:
                rows.append(Row("note", label=f"  {info.terms_url}"))
            if info.signup_url:
                rows.append(Row("note", label=f"  Cuenta/clave: {info.signup_url}"))
        for info in infos:
            if info.signup_url and info not in terms:
                rows.append(
                    Row("note", label=f"  {info.label}: obtén la clave en {info.signup_url}")
                )
        rows.append(Row("note", label=""))
        profiles = deploy.get("COMPOSE_PROFILES") or "(vacío)"
        rows.append(Row("note", label=f"Perfiles que se escribirán: {profiles}"))
        rows.append(
            Row("button", label="Modos de carga (embedded/sidecar/modal/disabled)…", action="modes")
        )
        if standalone:
            rows.append(Row("button", label="Guardar engines", action="save_engines"))
        rows.append(Row("button", label="Volver a engines", action="back"))
        return rows

    def on_change(app_: App, row: Row) -> None:
        if row.action.startswith("env:"):
            key = row.action.split(":", 1)[1]
            value = row.value.strip() if row.secret else row.value
            gateway.set(key, value)
            app_.say(f"{key} guardado en memoria")
            return
        if row.action.startswith("terms:"):
            key = row.action.split(":", 1)[1]
            accepted = _accepted_terms(ctx)
            if row.value.strip().lower() in ("true", "1", "yes"):
                accepted.append(key)
            else:
                accepted = [name for name in accepted if name != key]
            _store_accepted_terms(ctx, accepted)
            app_.say(
                f"Términos de {key} {'aceptados' if key in accepted else 'retirados'} en memoria",
                "ok" if key in accepted else "warn",
            )

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action == "modes":
            app_.push(runtime_modes_screen(app_, ctx, standalone=standalone))
            return
        if row.action == "save_engines":
            app_.modal = _confirm_engine_write(app_, ctx)

    return Screen(
        title="Requisitos de engines",
        subtitle="Contenedores, GPU, credenciales y términos",
        rows=build(),
        footer="F1 Ayuda   Enter Editar   a Avanzados   Esc Atrás   F10 Salir",
        on_change=on_change,
        on_enter=on_enter,
        on_activate=activate,
        on_help=lambda app_: app_.info(
            "Requisitos de engines",
            "El asistente añade perfiles Compose y URLs de sidecar automáticamente.",
            "GATEWAY_GPU=1 añade el overlay GPU al arrancar.",
            "Las claves pendientes avisan pero no bloquean el guardado.",
            "Los términos gated de HuggingFace se aceptan también en la web.",
        ),
    )


def tts_screen(
    app: App,
    ctx: Context,
    *,
    edit_mode: bool = False,
    standalone: bool = False,
) -> Screen:
    """Build the guided TTS engine, routing and model screen."""
    gateway = ctx.envs["gateway"]
    _sync_engine_requirements(ctx)
    warning = Row("note", label="", result="")
    requirement_row = Row("note", label="Requisitos: " + _requirement_summary(ctx), result="ok")
    engine = gateway.get("TTS_ENGINE").strip() or "routed"
    engine_row = Row(
        "choice",
        label="Engine principal",
        value=engine,
        options=list(ENGINE_INFO) + ["routed"],
        help="routed reparte por idioma; un engine fijo se usa para todo.",
        action="TTS_ENGINE",
    )
    device_row = Row(
        "choice",
        label="Dispositivo",
        value=gateway.get("TTS_DEVICE").strip() or "cuda:0",
        options=["cuda:0", "cpu"],
        help="cuda:0 exige el overlay GPU; cpu funciona sin gráfica.",
        action="TTS_DEVICE",
    )
    extra_row = Row(
        "multi",
        label="Engines disponibles (añadir/quitar)",
        value=gateway.get("TTS_EXTRA_ENGINES").strip(),
        options=list(EXTRA_ENGINE_OPTIONS),
        option_labels={key: engine_option_label(key) for key in EXTRA_ENGINE_OPTIONS},
        help=(
            "Espacio añade o quita engines para pins explícitos con routed; "
            "GPU = necesita gráfica; ca/es/fr/en ✓/✗ = idioma base soportado."
        ),
        action="TTS_EXTRA_ENGINES",
    )
    map_row = Row(
        "text",
        label="Mapa idioma→engine",
        value=gateway.get("TTS_ENGINES").strip(),
        help="JSON editable; el editor valida idiomas y engines.",
        action="TTS_ENGINES",
    )
    models_row = Row(
        "multi",
        label="Modelos a descargar",
        value=gateway.get("TTS_MODEL_DOWNLOADS").strip(),
        options=list(MODEL_OPTIONS),
        help="Marca varios checkpoints locales; los cloud no descargan pesos.",
        action="TTS_MODEL_DOWNLOADS",
    )
    rows: list[Row] = [
        Row("note", label="Catálogo de engines (dispositivo · clonado · idiomas):"),
        *[Row("note", label=line) for line in _tts_catalog_lines()],
        Row("note", label=""),
        Row("heading", label="Selección"),
        engine_row,
        device_row,
        warning,
        extra_row,
        Row("note", label=""),
        Row("heading", label="Routed"),
        map_row,
        Row("button", label="Aplicar preset routed…", action="presets"),
        Row("button", label="Editar mapa idioma→engine…", action="map"),
        Row("note", label=""),
        Row("heading", label="Modelos locales"),
        models_row,
        Row("note", label=""),
        Row("heading", label="Requisitos de infraestructura"),
        requirement_row,
        Row("button", label="Revisar contenedores, API y términos…", action="requirements"),
        Row("button", label="Modos de carga (embedded/sidecar/modal/disabled)…", action="modes"),
        Row("note", label=""),
        Row("heading", label="Seguridad"),
        Row("button", label="Resolver conflictos de engines…", action="fix_conflicts"),
        Row("button", label="Aplicar configuración segura (CPU)", action="safe"),
        Row("button", label="Explicación de la configuración TTS", action="explain"),
    ]
    if standalone:
        rows.extend(
            [
                Row("button", label="Guardar engines", action="save_engines"),
                Row("button", label="Salir sin guardar", action="quit"),
            ]
        )
    else:
        rows.append(Row("button", label="Volver a gateway", action="back"))
    rows_by_key = {
        "TTS_ENGINE": engine_row,
        "TTS_DEVICE": device_row,
        "TTS_EXTRA_ENGINES": extra_row,
        "TTS_ENGINES": map_row,
        "TTS_MODEL_DOWNLOADS": models_row,
    }
    _apply_gpu_lock(app, ctx, rows_by_key, warning)
    _sync_required_models(ctx, rows_by_key)

    def on_change(app_: App, row: Row) -> None:
        key = row.action
        if not key.startswith("TTS_"):
            return
        value = (
            row.value.strip()
            if key in {"TTS_ENGINES", "TTS_EXTRA_ENGINES", "TTS_MODEL_DOWNLOADS"}
            else row.value
        )
        gateway.set(key, value)
        if key in {"TTS_ENGINE", "TTS_DEVICE", "TTS_ENGINES", "TTS_EXTRA_ENGINES"}:
            _apply_gpu_lock(app_, ctx, rows_by_key, warning)
            _sync_required_models(ctx, rows_by_key)
            _sync_gateway_docker_extras(ctx)
            _sync_engine_requirements(ctx)
            requirement_row.label = "Requisitos: " + _requirement_summary(ctx)

    def activate(app_: App, row: Row) -> None:
        if row.action == "presets":
            _open_tts_presets(app_, ctx, edit_mode=edit_mode, standalone=standalone)
            return
        if row.action == "map":
            app_.push(tts_map_screen(app_, ctx, edit_mode=edit_mode, standalone=standalone))
            return
        if row.action == "requirements":
            app_.push(
                engine_requirements_screen(app_, ctx, edit_mode=edit_mode, standalone=standalone)
            )
            return
        if row.action == "modes":
            app_.push(runtime_modes_screen(app_, ctx, standalone=standalone))
            return
        if row.action == "fix_conflicts":
            app_.push(
                repair_screen(
                    app_,
                    ctx,
                    on_done=lambda active: active.replace(
                        tts_screen(active, ctx, edit_mode=edit_mode, standalone=standalone)
                    ),
                )
            )
            return
        if row.action == "safe":
            _apply_routed_preset(ctx, ROUTED_PRESETS[0])
            app_.replace(tts_screen(app_, ctx, edit_mode=edit_mode, standalone=standalone))
            app_.say(f"Configuración segura aplicada: {ROUTED_PRESETS[0].label}", "ok")
            return
        if row.action == "explain":
            app_.info("Configuración TTS", *_tts_help_lines())
            return
        if row.action == "save_engines":
            app_.modal = _confirm_engine_write(app_, ctx)
            return
        if row.action == "quit":
            app_.should_quit = True
            return
        if row.action == "back":
            _back_to_gateway(app_, ctx, edit_mode=edit_mode)

    return Screen(
        title="Engines TTS" if standalone else "Asistente TTS",
        subtitle=(
            "Contenedores, GPU, API, términos, routed y modelos"
            if standalone
            else "Engines, GPU/cloud, clonado, idiomas, routed y modelos"
        ),
        rows=rows,
        footer="F1 Ayuda   Enter Editar   Espacio Marcar   Esc Salir   F10 Salir",
        on_change=on_change,
        on_activate=activate,
        on_help=lambda app_: app_.info("Configuración TTS", *_tts_help_lines()),
    )


def tts_map_screen(
    app: App, ctx: Context, *, edit_mode: bool = False, standalone: bool = False
) -> Screen:
    """Edit the language→engine map without hand-writing JSON."""
    gateway = ctx.envs["gateway"]
    parsed = parse_engine_map(gateway.get("TTS_ENGINES"))
    routing = parsed or {}
    state = {"new": "", "invalid": parsed is None}
    engine_options = list(ENGINE_INFO)

    def default_engine() -> str:
        current = gateway.get("TTS_ENGINE").strip()
        if current in ENGINE_INFO:
            return current
        fallback = routing.get("_default") or next(iter(routing.values()), "")
        return fallback if fallback in ENGINE_INFO else "pocket_tts"

    def build() -> list[Row]:
        rows: list[Row] = [
            Row(
                "note",
                label="Asigna un engine a cada idioma ISO. _default cubre los no listados.",
            ),
            Row("note", label=f"Mapa actual: {render_engine_map(routing)}"),
        ]
        if state["invalid"]:
            rows.append(
                Row(
                    "note",
                    label="⚠ El TTS_ENGINES actual no es JSON válido; guardar lo reemplazará.",
                    result="warn",
                )
            )
        rows.append(Row("note", label=""))
        for code, selected in routing.items():
            rows.append(
                Row(
                    "choice",
                    label=f"Idioma {code}",
                    value=selected,
                    options=engine_options,
                    help=f"Engine para «{code}»; Enter abre la lista.",
                    action=f"lang:{code}",
                )
            )
        if not routing:
            rows.append(Row("note", label="El mapa está vacío.", result="warn"))
        rows.extend(
            [
                Row(
                    "text",
                    label="Nuevo idioma (ISO)",
                    value=state["new"],
                    help="Código de dos o tres letras; Enter edita y luego «Añadir idioma».",
                    action="new",
                ),
                Row("button", label="Añadir idioma", action="add"),
                Row("button", label="Quitar idioma…", action="remove"),
                Row("button", label="Guardar mapa", action="save"),
                Row("button", label="Volver sin guardar", action="back"),
            ]
        )
        return rows

    def on_change(app_: App, row: Row) -> None:
        if row.action == "new":
            state["new"] = row.value
        elif row.action.startswith("lang:"):
            routing[row.action.split(":", 1)[1]] = row.value

    def activate(app_: App, row: Row) -> None:
        if row.action == "add":
            code = state["new"].strip().lower()
            if not valid_language_code(code):
                app_.say("Idioma inválido: usa un código ISO de 2-3 letras", "bad")
                return
            if code in routing:
                app_.say(f"El idioma {code} ya está en el mapa", "warn")
                return
            routing[code] = default_engine()
            state["new"] = ""
            state["invalid"] = False
            app_.current.rows = build()
            app_.refocus()
            app_.say(f"Añadido {code} → {routing[code]}", "ok")
            return
        if row.action == "remove":
            if not routing:
                app_.say("No hay idiomas que quitar", "warn")
                return

            def remove(app2: App, code: str) -> None:
                app2.close_modal()
                routing.pop(code, None)
                state["invalid"] = False
                app2.current.rows = build()
                app2.refocus()
                app2.say(f"Idioma {code} eliminado", "ok")

            app_.modal = Picker(
                "Quitar idioma",
                list(routing),
                "",
                remove,
                "Enter elimina el idioma seleccionado",
            )
            return
        if row.action == "save":
            gateway.set("TTS_ENGINES", render_engine_map(routing))
            _sync_gateway_docker_extras(ctx)
            _sync_engine_requirements(ctx)
            _back_to_tts(app_, ctx, edit_mode=edit_mode, standalone=standalone)
            app_.say("Mapa idioma→engine guardado en memoria", "ok")
            return
        if row.action == "back":
            _back_to_tts(app_, ctx, edit_mode=edit_mode, standalone=standalone)

    return Screen(
        title="Mapa idioma→engine",
        subtitle="Editor de TTS_ENGINES",
        rows=build(),
        footer="F1 Ayuda   Enter Editar   Esc Atrás   F10 Salir",
        on_change=on_change,
        on_activate=activate,
        on_help=lambda app_: app_.info("Mapa idioma→engine", *_tts_help_lines()),
    )


def _shown(key: str, value: str) -> str:
    return _mask(value) if key in _SECRET_KEYS else (value or "(vacío)")


def review_screen(app: App, ctx: Context) -> Screen:
    """Build the pending diff summary. Rebuilt every time it is entered."""

    def build() -> list[Row]:
        _sync_gateway_docker_extras(ctx)
        _sync_engine_requirements(ctx)
        rows = [
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
        conflicts = _shared_key_conflicts(ctx)
        if conflicts:
            rows.extend(
                [
                    Row("note", label=""),
                    Row(
                        "note",
                        label=f"Clave del gateway distinta en {', '.join(conflicts)}: "
                        "el gateway respondería 401",
                        result="bad",
                    ),
                    Row("button", label="Unificar la clave del gateway", action="unify"),
                ]
            )
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
        if row.action == "unify":
            changed = _unify_shared_key(ctx)
            app_.say(f"Clave del gateway unificada en {changed} archivo(s)", "ok")
            app_.current.rows = build()
            app_.refocus()
            return
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=False))
            return
        if row.action == "write":
            app_.modal = _confirm_write(app_, ctx)

    return Screen(
        title="Paso 4 · revisión",
        subtitle="Diff de los tres archivos",
        rows=build(),
        footer="F1 Ayuda   Enter Escribir   Esc Atrás   F10 Salir",
        progress="4/6",
        on_enter=on_enter,
        on_activate=activate,
    )


def _write_env(app: App, env: EnvFile, *, reload: bool) -> tuple[bool, Path | None, list[str]]:
    """Write one env file, reporting permission/disk errors inside the TUI."""
    names = env.changed_keys()
    try:
        backup = env.write()
    except OSError as exc:
        app.say(f"No se pudo escribir {env.key_path}: {exc}", "bad")
        app.info(
            "Error al guardar",
            f"{env.key_path}: {exc}",
            "El asistente sigue abierto. Revisa permisos y espacio libre, y reintenta.",
        )
        return False, None, names
    if reload:
        env.reload_from_disk()
    app.say(f"Escrito {env.key_path}: {len(names)} valor(es)", "ok")
    return True, backup, names


def _write_setup_artifacts(app: App, ctx: Context, *, written: list[str]) -> None:
    """Create the example collection and the secret-free review report."""
    news = ctx.envs.get("news")
    if news is not None:
        created = ensure_example_collection(
            backend=news.get("TTS_DEFAULT_BACKEND"),
            language=news.get("TTS_LANGUAGE"),
            voice=news.get("TTS_VOICE"),
        )
        if created is not None:
            app.say(f"Creada colección de ejemplo: {created.relative_to(REPO_ROOT)}", "ok")
    try:
        checks = validation.validate(
            ctx.envs,
            ctx.report,
            will_write=False,
            profile=ctx.profile.key,
        )
        path = write_config_report(
            ctx.envs,
            ctx.report,
            profile=ctx.profile,
            method=ctx.method,
            checks=checks,
            written=written,
            repairs=ctx.repairs,
            manual=ctx.manual,
        )
    except OSError as exc:
        app.say(f"No se pudo escribir el informe de setup: {exc}", "bad")
        return
    ctx.report_path = str(path)
    app.say(f"Informe sin secretos: {path.relative_to(REPO_ROOT)}", "ok")


def _write_engine_report(app: App, ctx: Context) -> None:
    """Write the secret-free report after an engines-only save."""
    try:
        checks = validation.validate(
            ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key
        )
        path = write_config_report(
            ctx.envs,
            ctx.report,
            profile=ctx.profile,
            method=ctx.method,
            checks=checks,
            written=ctx.written,
            repairs=ctx.repairs,
            manual=ctx.manual,
        )
    except OSError as exc:
        app.say(f"No se pudo escribir el informe de setup: {exc}", "bad")
        return
    ctx.report_path = str(path)


def _confirm_engine_write(app: App, ctx: Context):
    """Return the confirmation dialog used by the engines-only flow."""

    def do_write(app_: App) -> None:
        app_.close_modal()
        _sync_gateway_docker_extras(ctx)
        _sync_engine_requirements(ctx)
        written: list[str] = []
        for key in ("deploy", "gateway"):
            env = ctx.envs[key]
            if not env.changed_keys() and env.exists:
                continue
            ok, backup, _names = _write_env(app_, env, reload=True)
            if not ok:
                ctx.written = written
                return
            written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
        ctx.written = written
        if written:
            app_.say(f"Escrito: {', '.join(written)}", "ok")
        else:
            app_.say("No había nada que escribir", "ok")
        _write_engine_report(app_, ctx)
        app_.push(engine_done_screen(app_, ctx))

    changes = sum(len(ctx.envs[key].diff_against_disk()) for key in ("deploy", "gateway"))
    return Dialog(
        "Guardar engines",
        [
            f"Se escribirán hasta {changes} valor(es) en deploy/.env y gateway/.env.",
            "No se modifica news/.env ni se instalan dependencias.",
            "Las copias previas guardan .bak-<fecha>.",
        ],
        [("Cancelar", lambda a: a.close_modal()), ("Guardar", do_write)],
        index=1,
    )


def engine_done_screen(app: App, ctx: Context) -> Screen:
    """Closing screen for ``make engines`` with the next deployment commands."""
    written = ", ".join(ctx.written) or "sin cambios"
    rows = [
        Row("note", label=f"Guardado: {written}", result="ok"),
        Row("note", label="Requisitos: " + _requirement_summary(ctx)),
        Row("note", label=""),
        Row("note", label="Siguientes comandos:", result="ok"),
        Row("note", label="  make up        · reconstruye y arranca el stack"),
        Row("note", label="  make health    · comprueba API y gateway"),
        Row("note", label="  make engines   · vuelve a esta configuración"),
    ]
    if ctx.report_path:
        try:
            report_label = Path(ctx.report_path).relative_to(REPO_ROOT)
        except ValueError:
            report_label = Path(ctx.report_path)
        rows.append(Row("note", label=f"Informe sin secretos: {report_label}", result="ok"))
    rows.extend(
        [
            Row("note", label=""),
            Row("button", label="Validar configuración", action="validate"),
            Row("button", label="Salir", action="quit"),
        ]
    )

    def activate(app_: App, row: Row) -> None:
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=bool(ctx.written)))
            return
        if row.action == "quit":
            app_.should_quit = True

    return Screen(
        title="Engines guardados",
        subtitle="Configuración de contenedores, GPU y credenciales",
        rows=rows,
        footer="F1 Ayuda   Enter Ejecutar   F10 Salir",
        on_activate=activate,
    )


def _confirm_write(app: App, ctx: Context):
    """Return the confirmation dialog that writes the env files."""
    from .curses_ui import Dialog

    def do_write(app_: App) -> None:
        app_.close_modal()
        _sync_gateway_docker_extras(ctx)
        _sync_engine_requirements(ctx)
        written: list[str] = []
        for key in _ORDER:
            env = ctx.envs[key]
            if not env.changed_keys() and env.exists:
                continue
            ok, backup, _names = _write_env(app_, env, reload=True)
            if not ok:
                ctx.written = written
                return
            written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
        ctx.written = written
        if written:
            app_.say(f"Escrito: {', '.join(written)}", "ok")
        else:
            app_.say("No había nada que escribir", "ok")
        _write_setup_artifacts(app_, ctx, written=written)
        app_.push(apply_screen(app_, ctx))

    changes = sum(len(ctx.envs[key].diff_against_disk()) for key in _ORDER)
    return Dialog(
        "Escribir archivos",
        [
            f"Se escribirán hasta {changes} valor(es) en {len(_ORDER)} archivo(s).",
            "Las copias previas guardan .bak-<fecha>.",
        ],
        [("Cancelar", lambda a: a.close_modal()), ("Escribir", do_write)],
        index=1,
    )


def _confirm_write_one(app: App, ctx: Context, file_key: str):
    """Return the confirmation dialog that writes a single env file."""
    from .curses_ui import Dialog

    env = ctx.envs[file_key]

    def do_save(app_: App) -> None:
        app_.close_modal()
        _sync_gateway_docker_extras(ctx)
        _sync_engine_requirements(ctx)
        if not env.changed_keys():
            app_.say("Sin cambios que escribir", "")
            return
        ok, backup, names = _write_env(app_, env, reload=True)
        if not ok:
            return
        app_.info(
            "Guardado",
            f"{env.key_path}: {len(names)} valor(es) escritos.",
            f"Copia previa: {backup.name}" if backup else "Sin copia previa (archivo nuevo).",
        )
        written = env.key_path + (f" (copia: {backup.name})" if backup else "")
        ctx.written.append(written)
        _write_setup_artifacts(app_, ctx, written=list(ctx.written))

    if not env.changed_keys():
        return None

    return Dialog(
        "Guardar en disco",
        [
            f"{env.key_path}: {len(env.changed_keys())} valor(es) por escribir.",
            "Se guarda una copia previa .bak-<fecha>.",
        ],
        [("Cancelar", lambda a: a.close_modal()), ("Guardar", do_save)],
        index=1,
    )


def edit_picker_screen(app: App, ctx: Context) -> Screen:
    """Build the menu that routes to one env file for direct editing."""

    def build() -> list[Row]:
        rows = [
            Row("note", label="Abre un archivo, cambia valores y guarda en disco."),
            Row("note", label=""),
        ]
        for target in env_paths():
            env = ctx.envs[target.key]
            pending = len(env.diff_against_disk())
            rows.append(
                Row(
                    "button",
                    label=f"{target.label}",
                    value=target.key_path,
                    action=target.key,
                )
            )
            if pending:
                rows.append(
                    Row(
                        "note",
                        label=f"  {target.key_path}: {pending} cambio(s) sin guardar",
                        result="warn",
                    )
                )
        rows.append(Row("note", label=""))
        rows.append(Row("button", label="Atrás", action="back"))
        return rows

    def on_enter(app_: App) -> None:
        app_.current.rows = build()

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action in ctx.envs:
            app_.push(env_screen(app_, ctx, row.action, edit_mode=True))

    return Screen(
        title="Editar configuración",
        subtitle="Elige un archivo",
        rows=build(),
        footer="F1 Ayuda   Enter Abrir   Esc Atrás   F10 Salir",
        on_enter=on_enter,
        on_activate=activate,
    )


def import_screen(app: App, ctx: Context) -> Screen:
    """Build the screen that pulls values from an external .env file."""
    path_row = Row(
        "text",
        label="Ruta del .env origen",
        value="",
        help="Ruta absoluta o relativa al repo; solo importan claves ya existentes",
    )
    rows = [
        Row(
            "note",
            label="Sube los valores de otro .env a los tres archivos (claves conocidas).",
        ),
        Row("note", label=""),
        path_row,
        Row("note", label=""),
        Row("button", label="Importar al archivo destino", action="import"),
        Row("button", label="Atrás", action="back"),
    ]

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action != "import":
            return
        raw = path_row.value.strip()
        if not raw:
            app_.say("Escribe primero una ruta en el campo de arriba", "warn")
            return
        source = Path(raw).expanduser()
        if not source.is_absolute() and not source.exists():
            source = REPO_ROOT / source
        if not source.is_file():
            app_.say(f"No existe el archivo {source}", "bad")
            return
        text = source.read_text(encoding="utf-8", errors="replace")
        values = transfer.parse_env_text(text)
        if not values:
            app_.say("El archivo no contiene claves KEY=VALUE", "warn")
            return
        from .transfer import import_values

        applied, unknown = import_values(ctx, values)
        summary = list(applied[:12])
        if len(applied) > 12:
            summary.append(f"… y {len(applied) - 12} más")
        lines = [f"Origen: {source}"]
        if applied:
            lines.append(f"{len(applied)} valor(es) aplicados en memoria:")
        for name in summary:
            line = name if name.startswith("…") else f"  {name}"
            lines.append(line)
        if not applied:
            lines.append("Ningún valor cambió: todo coincide con lo configurado.")
        if unknown:
            shown = ", ".join(unknown[:10]) + (", …" if len(unknown) > 10 else "")
            lines.append(f"{len(unknown)} clave(s) sin destino (ignoradas): {shown}")
        lines.append("(guarda con «Editar los .env» o el asistente)")
        app_.info("Importación", *lines)
        app_.say(f"Importados {len(applied)} valor(es) · {len(unknown)} ignorados", "ok")
        app_.pop()

    return Screen(
        title="Importar · .env externo",
        subtitle="Claves ya presentes en los tres archivos",
        rows=rows,
        footer="F1 Ayuda   Enter Editar/Aceptar   Esc Atrás   F10 Salir",
        on_activate=activate,
    )


def export_screen(app: App, ctx: Context) -> Screen:
    """Build the screen that dumps the three env files to one archive."""
    path_row = Row(
        "text",
        label="Ruta del archivo destino",
        value=str(REPO_ROOT / ".env.newsvoice"),
        help="Ahí se escribirán todos los valores activos (incluye secretos)",
    )
    rows = [
        Row("note", label="Escribe los valores activos de los tres .env, agrupados por archivo."),
        Row(
            "note",
            label="Atención: el resultado incluye secretos en texto claro.",
            result="warn",
        ),
        Row("note", label=""),
        path_row,
        Row("note", label=""),
        Row("button", label="Exportar al archivo destino", action="export"),
        Row("button", label="Atrás", action="back"),
    ]

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action != "export":
            return
        raw = path_row.value.strip()
        if not raw:
            app_.say("Escribe primero una ruta en el campo de arriba", "warn")
            return
        destination = Path(raw).expanduser()
        if not destination.is_absolute() and not destination.name.startswith("."):
            destination = REPO_ROOT / destination
        if destination.resolve() in (t.path.resolve() for t in env_paths()):
            app_.say("No exportes encima de un .env del proyecto", "bad")
            return
        transfer.export_write(ctx, destination)
        app_.say(f"Exportado a {destination}", "ok")
        app_.info(
            "Exportación",
            f"Escrito: {destination}",
            f"Valores activos de {len(ctx.envs)} archivo(s), agrupados por sección.",
            "Recuerda que el archivo contiene secretos en claro.",
        )
        app_.pop()

    return Screen(
        title="Exportar · configuración actual",
        subtitle="Los tres .env en un solo archivo",
        rows=rows,
        footer="F1 Ayuda   Enter Editar/Aceptar   Esc Atrás   F10 Salir",
        on_activate=activate,
    )


def _dependency_plan(ctx: Context) -> list[deps.Step]:
    """Return the install plan with the extras required by the selected engines."""
    engine, engine_map, extra = _gateway_selection(ctx)
    return deps.plan(
        ctx.method,
        gateway_extras=gateway_pip_extras(engine, engine_map, extra),
    )


def apply_screen(app: App, ctx: Context) -> Screen:
    """Build the dependency installation step."""

    def build() -> list[Row]:
        steps = _dependency_plan(ctx)
        rows = [
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
        steps = _dependency_plan(ctx)
        if not steps:
            app_.say("Nada que instalar con este método")
            return
        verify_steps = deps.verify(ctx.method)
        ok = _run_steps_with_progress(
            app_,
            steps,
            title="Instalando dependencias",
            subtitle=(f"Método: {ctx.method} · {len(steps) + len(verify_steps)} acción(es)"),
            followup=verify_steps,
        )
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
        subtitle="Instalación del entorno del proyecto",
        rows=build(),
        footer="F1 Ayuda   Enter Ejecutar   Esc Atrás   F10 Salir",
        progress="5/6",
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
        if ctx.repairs:
            rows.append(
                Row("note", label=f"Reparaciones aplicadas: {len(ctx.repairs)}", result="ok")
            )
        if ctx.manual:
            rows.append(
                Row("note", label=f"Pendiente manual: {len(ctx.manual)} problema(s)", result="warn")
            )
        rows.append(Row("button", label="Solucionar todo…", action="fix_all"))
        rows.append(Row("button", label="Solventar problemas con IA…", action="ai_fix"))
        conflicts = _shared_key_conflicts(ctx)
        if conflicts:
            rows.extend(
                [
                    Row(
                        "note",
                        label=f"Corrige la clave antes de desplegar: {', '.join(conflicts)}",
                        result="bad",
                    ),
                    Row("button", label="Unificar la clave del gateway", action="unify"),
                ]
            )
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
        if row.action == "fix_all":
            app_.push(
                repair_screen(
                    app_,
                    ctx,
                    all_problems=True,
                    on_done=lambda active: active.replace(
                        validate_screen(active, ctx, write_mode=write_mode)
                    ),
                )
            )
            return
        if row.action == "ai_fix":
            app_.push(ai_fix_screen(app_, ctx, write_mode=write_mode))
            return
        if row.action == "unify":
            changed = _unify_shared_key(ctx)
            app_.current.rows = build()
            app_.refocus()
            app_.say(f"Clave del gateway unificada en {changed} archivo(s)", "ok")
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
        subtitle="Reglas compartidas por la TUI y la CLI",
        rows=build(),
        footer="F1 Ayuda   Enter Ejecutar   Esc Atrás   F10 Salir",
        progress="6/6",
        on_enter=on_enter,
        on_activate=activate,
    )


def ai_fix_screen(app: App, ctx: Context, *, write_mode: bool) -> Screen:
    """Configure and run the ephemeral AI repair agent."""
    from .ai_fix import run_ai_fix

    news = ctx.envs["news"]
    fields = {field.key: field for field in _fields("news")}
    url_row = Row(
        "text",
        label="URL base IA (incluye /v1)",
        value=_value_for(ctx, fields["AI_FIX_API_BASE"]) if "AI_FIX_API_BASE" in fields else "",
        help="Endpoint OpenAI-compatible usado solo por el diagnóstico.",
        action="aifix_base",
    )
    model_row = Row(
        "text",
        label="Modelo IA",
        value=_value_for(ctx, fields["AI_FIX_MODEL"]) if "AI_FIX_MODEL" in fields else "",
        help="Id exacto del modelo servido por la URL anterior.",
        action="aifix_model",
    )
    key_row = Row(
        "secret",
        label="Clave API IA (opcional)",
        value=news.get("AI_FIX_API_KEY"),
        help="Se guarda en apps/news/.env y nunca se envía en el prompt.",
        action="aifix_key",
    )
    rows = [
        Row("note", label="El agente se descarga con HTTPS + SHA256, se ejecuta en /tmp"),
        Row("note", label="y se desinstala al terminar. Los cambios se muestran antes de aplicar."),
        Row("note", label=""),
        url_row,
        model_row,
        key_row,
        Row("note", label=""),
        Row("button", label="Diagnosticar y aplicar correcciones…", action="run"),
        Row("button", label="Solo diagnosticar (no aplicar)…", action="dry"),
        Row("button", label="Atrás", action="back"),
    ]

    def on_change(app_: App, row: Row) -> None:
        keys = {
            "aifix_base": "AI_FIX_API_BASE",
            "aifix_model": "AI_FIX_MODEL",
            "aifix_key": "AI_FIX_API_KEY",
        }
        key = keys.get(row.action)
        if key is not None:
            news.set(key, row.value.strip())

    def activate(app_: App, row: Row) -> None:
        if row.action == "back":
            app_.pop()
            return
        if row.action not in ("run", "dry"):
            return
        for source, key in (
            (url_row, "AI_FIX_API_BASE"),
            (model_row, "AI_FIX_MODEL"),
            (key_row, "AI_FIX_API_KEY"),
        ):
            news.set(key, source.value.strip())
        dialog = Dialog(
            "Solventar problemas con IA",
            ["Preparando diagnosis…"],
            [("Cerrar", lambda active: active.close_modal())],
        )
        app_.modal = dialog

        def emit(message: str) -> None:
            dialog.lines.append(message.rstrip())
            dialog.lines = dialog.lines[-40:]
            app_.say(message.strip()[:200])
            app_.redraw()

        result = run_ai_fix(
            ctx,
            dry_run=row.action == "dry",
            emit=emit,
            confirm=lambda question: row.action == "run",
        )
        dialog.lines.append("")
        dialog.lines.append(f"Estado: {result.status}")
        if result.applied:
            dialog.lines.append("Aplicado: " + ", ".join(result.applied))
        if result.error:
            dialog.lines.append(f"Error: {result.error}")
        dialog.lines.append(f"Agente desinstalado: {'sí' if result.agent_removed else 'no'}")
        dialog.lines = dialog.lines[-40:]
        app_.say("Diagnóstico con IA terminado", "ok" if result.status == "ok" else "bad")
        app_.redraw()

    return Screen(
        title="Solventar problemas con IA",
        subtitle="Agente efímero · descarga verificada · cambios revisables",
        rows=rows,
        footer="F1 Ayuda   Enter Editar   Esc Atrás   F10 Salir",
        progress="6/6",
        on_change=on_change,
        on_activate=activate,
    )


def final_screen(app: App, ctx: Context) -> Screen:
    """Build the closing screen with the hand-off instructions."""
    written = ", ".join(ctx.written) or "los archivos ya existentes"
    rows = [
        Row("note", label=f"Escrito: {written}", result="ok"),
        Row("note", label=f"Dependencias: {ctx.install_result or 'pendientes'}"),
    ]
    if ctx.report_path:
        try:
            report_label = Path(ctx.report_path).relative_to(REPO_ROOT)
        except ValueError:
            report_label = Path(ctx.report_path)
        rows.append(Row("note", label=f"Informe sin secretos: {report_label}", result="ok"))
    if ctx.repairs:
        rows.append(Row("note", label=f"Reparaciones aplicadas: {len(ctx.repairs)}", result="ok"))
    if ctx.manual:
        rows.append(
            Row("note", label=f"Pendiente manual: {len(ctx.manual)} problema(s)", result="warn")
        )
    rows.extend(
        [
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
    )

    def activate(app_: App, row: Row) -> None:
        if row.action == "validate":
            app_.push(validate_screen(app_, ctx, write_mode=bool(ctx.written)))
        elif row.action == "help":
            app_.info(
                "Ayuda",
                "↑/↓ o Tab mover · Enter seleccionar/editar",
                "Esc atrás · F10 o q salir",
                "g generar secreto · a mostrar avanzados",
                "Ctrl+R valor por defecto · Espacio sí/no",
            )
        elif row.action == "quit":
            app_.should_quit = True

    return Screen(
        title="Configuración lista",
        subtitle=ctx.profile.label,
        rows=rows,
        footer="F1 Ayuda   Enter Ejecutar   F10 Salir",
        progress="Fin",
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
    app = App("newsvoice setup")
    app.push(welcome_screen(app, ctx))
    app.push(external_guide_screen(app))
    return app


def build_engines_app(ctx: Context | None = None) -> App:
    """Create an app that opens directly on the engines assistant."""
    ctx = ctx or load_context()
    app = App("newsvoice engines")
    app.push(tts_screen(app, ctx, standalone=True))
    return app
