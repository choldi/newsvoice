"""Line oriented fallback for the wizard.

Used when there is no usable terminal (piped stdin, dumb TERM, missing
curses). It walks the very same screens as the TUI, one prompt per line, so a
headless setup still gets the assisted configuration.
"""

from __future__ import annotations

import sys

from . import deps
from . import validate as validation
from .app import (
    Context,
    _apply_profile,
    _dependency_plan,
    _mask,
    _shared_key_conflicts,
    _shown,
    _sync_engine_requirements,
    _sync_gateway_docker_extras,
    _sync_shared_key,
    _unify_shared_key,
    load_context,
)
from .collections import ensure_example_collection
from .config_report import write_config_report
from .detect import detect, prerequisites
from .settings import (
    ENGINE_INFO,
    PROFILES,
    Field,
    config_requires_gpu,
    engine_terms,
    fields_for,
    join_multi,
    required_engine_keys,
    required_model_downloads,
    selected_engine_infos,
    split_multi,
)
from .util import REPO_ROOT

__all__ = ["run_engines", "run_plain"]

_ORDER = ("deploy", "news", "gateway")
_LINE = "-" * 68


def _read(prompt: str, default: str = "") -> str:
    """Read one line, falling back to ``default`` on EOF.

    Args:
        prompt: Text shown to the user.
        default: Value used when the input is empty.

    Returns:
        The answer, or ``default``.
    """
    try:
        raw = input(prompt)
    except (EOFError, OSError, KeyboardInterrupt):
        print()
        return default
    return raw.strip()


def _title(text: str) -> None:
    print(f"\n{_LINE}\n{text}\n{_LINE}")


def _confirm(question: str, *, assume_yes: bool) -> bool:
    """Ask a yes/no question, short-circuiting it with ``assume_yes``.

    Args:
        question: Prompt including the ``[s/N]`` hint.
        assume_yes: When True the question is skipped and answered yes.

    Returns:
        True when the user accepts.
    """
    if assume_yes:
        return True
    return _read(f"  {question} ").lower() in ("s", "sí", "si", "y", "yes")


def _choose(title: str, options: list[str], current: int = 0) -> int:
    """Present a numbered menu.

    Args:
        title: Menu heading.
        options: Entries to choose from.
        current: Pre-selected index (shown as the default).

    Returns:
        The selected index.
    """
    _title(title)
    for index, option in enumerate(options, start=1):
        mark = "*" if index - 1 == current else " "
        print(f" {mark} {index}. {option}")
    while True:
        answer = _read(f"Elige [1-{len(options)}] ({current + 1}): ")
        if not answer:
            return current
        try:
            picked = int(answer)
        except ValueError:
            print("  Escribe un número.")
            continue
        if 1 <= picked <= len(options):
            return picked - 1
        print(f"  Escribe un número entre 1 y {len(options)}.")


def _multi_prompt(field: Field, current: str) -> str:
    """Show a numbered checklist and return the selected values.

    Args:
        field: Multi-select field with its allowed options.
        current: Current comma-separated value.

    Returns:
        The new comma-separated value, or ``q`` / ``s`` to end or skip.
    """
    chosen = {item.strip() for item in current.split(",") if item.strip()}
    print("    opciones:")
    for index, option in enumerate(field.options, start=1):
        mark = "x" if option in chosen else " "
        print(f"      [{mark}] {index}. {option}")
    answer = _read(f"    [{current or 'vacío'}] números o valores separados por comas: ")
    if answer.lower() in {"q", "s"}:
        return answer.lower()
    if not answer:
        return current
    picked: list[str] = []
    for token in answer.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(field.options):
            picked.append(field.options[int(token) - 1])
        else:
            picked.append(token)
    return ",".join(dict.fromkeys(picked))


def run_plain(
    ctx: Context | None = None,
    *,
    method_hint: str = "",
    profile_hint: str = "",
    assume_yes: bool = False,
) -> int:
    """Run the whole wizard on stdin/stdout.

    Args:
        ctx: Pre-loaded context, detected when omitted.
        method_hint: Installation method id to skip its prompt.
        profile_hint: Profile id to skip its prompt.
        assume_yes: Answer every confirmation with yes (scripted runs).

    Returns:
        Process exit status (0 on success, 1 when a required step failed).
    """
    ctx = ctx or load_context()

    _title("newsvoice · instalador guiado (modo sin TUI)")
    print(f"Python {ctx.report.python}")
    for finding in ctx.report.findings:
        print(f"  [{finding.status:>4}] {finding.name}: {finding.detail}")

    # 0. prerequisites ------------------------------------------------------
    items = prerequisites(ctx.report)
    missing = [item for item in items if item.missing]
    _title("Paso 0 · requisitos del sistema")
    if not missing:
        print(f"  ✓ los {len(items)} requisitos están listos")
    for item in missing:
        print(f"  [{item.status:>4}] {item.name} — {item.detail}")
        print(f"          hace falta porque: {item.why}")
        for command in item.remedy:
            print(f"          $ {command}")
        if not item.remedy:
            print("          instalación manual (sin comando seguro)")
    automated = [item for item in missing if item.automated]
    if automated:
        if assume_yes:
            print(
                "  (modo --yes: no se instalan paquetes del sistema; ejecuta los comandos a mano)"
            )
        elif _confirm(
            f"¿Instalar ahora {len(automated)} requisito(s) con esos comandos? [s/N]:",
            assume_yes=False,
        ):
            steps = deps.prereq_steps(automated)
            deps.execute(
                steps,
                lambda status, title, detail: print(f"  [{status}] {title}: {detail}"),
            )
            ctx.report = detect()
            print("  → requisitos actualizados")
        else:
            print("  (pendiente: ejecuta los comandos anteriores a mano)")

    # 1. profile ------------------------------------------------------------
    if profile_hint:
        match = next((p for p in PROFILES if p.key == profile_hint), None)
        if match is None:
            print(f"Perfil desconocido: {profile_hint}", file=sys.stderr)
            return 2
        ctx.profile = match
    else:
        index = _choose(
            "Paso 1 · punto de partida",
            [f"{p.label} — {p.description}" for p in PROFILES],
        )
        ctx.profile = PROFILES[index]
    print(f"  → {ctx.profile.label}")
    if ctx.profile.key != "audit":
        _apply_profile(ctx)

    # 2. dependency method --------------------------------------------------
    if ctx.profile.key == "audit":
        ctx.method = "skip"
        print("  → modo solo revisión: no se instala ni se escribe nada")
        return _finish(ctx, write=False, assume_yes=assume_yes)

    if method_hint:
        ctx.method = method_hint
    else:
        index = _choose(
            "Paso 2 · instalación de dependencias",
            [f"{label} — {detail}" for _key, label, detail in deps.METHODS],
        )
        ctx.method = deps.METHODS[index][0]
    print(f"  → {ctx.method}")

    # 3. env files ----------------------------------------------------------
    for file_key in _ORDER:
        env = ctx.envs[file_key]
        _title(f"Paso 3 · {env.key_path}")
        print("  Pulsa Enter para conservar el valor actual.")
        print("  Escribe un valor nuevo, `g` para generar un secreto,")
        print("  `s` para saltar el archivo y `q` para terminar.")
        advanced_shown: bool | None = None
        for field_ in fields_for(file_key):
            if field_.advanced:
                if advanced_shown is None:
                    answer = _read("  ¿Ver también las opciones avanzadas? [s/N]: ")
                    advanced_shown = answer.lower() in ("s", "sí", "si", "y", "yes")
                if not advanced_shown:
                    continue
            current = env.get(field_.key).strip() or field_.default
            shown = _mask(current) if field_.kind in ("secret", "password") else current
            print(f"\n  {field_.label}  ({field_.key})")
            print(f"    {field_.help}")
            if field_.kind == "multi":
                answer = _multi_prompt(field_, current)
                if answer == "q":
                    return _finish(ctx, write=False, assume_yes=assume_yes)
                if answer == "s":
                    break
                env.set(field_.key, answer)
                continue
            answer = _read(f"    [{shown or 'vacío'}] ")
            if answer.lower() == "q":
                return _finish(ctx, write=False, assume_yes=assume_yes)
            if answer.lower() == "s":
                break
            if not answer:
                continue
            if answer.lower() == "g" and field_.generate:
                from .util import generate_secret

                answer = generate_secret()
            env.set(field_.key, answer)

    _sync_shared_key(ctx)
    _sync_gateway_docker_extras(ctx)

    # 4. review -------------------------------------------------------------
    _title("Paso 4 · revisión de cambios")
    conflicts = _shared_key_conflicts(ctx)
    if conflicts:
        print(f"  ✗ clave del gateway distinta en {', '.join(conflicts)}: respondería 401")
        if _confirm("¿Unificar la clave en los tres archivos? [s/N]:", assume_yes=True):
            print(f"  → unificada en {_unify_shared_key(ctx)} archivo(s)")
        else:
            print("  (se mantiene la diferencia: la validación la marcará como error)")
    total = 0
    for file_key in _ORDER:
        changes = ctx.envs[file_key].diff_against_disk()
        if not changes:
            print(f"  {ctx.envs[file_key].key_path}: sin cambios")
            continue
        print(f"  {ctx.envs[file_key].key_path}:")
        for name, old, new in changes:
            print(f"    {name}: {_shown(name, old)} → {_shown(name, new)}")
        total += len(changes)
    if total == 0:
        print("  No hay nada que escribir.")
    saved = False
    if _confirm("¿Escribir los archivos .env? [s/N]:", assume_yes=assume_yes):
        saved = True
        for file_key in _ORDER:
            env = ctx.envs[file_key]
            if not env.changed_keys() and env.exists:
                continue
            backup = env.write()
            ctx.written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
        news = ctx.envs.get("news")
        if news is not None:
            created = ensure_example_collection(
                backend=news.get("TTS_DEFAULT_BACKEND"),
                language=news.get("TTS_LANGUAGE"),
                voice=news.get("TTS_VOICE"),
            )
            if created is not None:
                print(f"  Colección de ejemplo: {created.relative_to(REPO_ROOT)}")
        print(f"  Escrito: {', '.join(ctx.written) or 'nada'}")

    return _finish(ctx, write=saved, assume_yes=assume_yes)


def _engine_field(key: str) -> Field | None:
    """Return one gateway field used by the engines-only line flow."""
    return next((field_ for field_ in fields_for("gateway") if field_.key == key), None)


def _set_required_models(ctx: Context) -> None:
    """Keep the mandatory model checkpoints in TTS_MODEL_DOWNLOADS."""
    gateway = ctx.envs["gateway"]
    engine = gateway.get("TTS_ENGINE")
    engine_map = gateway.get("TTS_ENGINES")
    extra = gateway.get("TTS_EXTRA_ENGINES")
    required = required_model_downloads(engine, engine_map, extra)
    current = split_multi(gateway.get("TTS_MODEL_DOWNLOADS"))
    for model in required:
        if model not in current:
            current.append(model)
    gateway.set("TTS_MODEL_DOWNLOADS", join_multi(current))


def run_engines(ctx: Context | None = None, *, assume_yes: bool = False) -> int:
    """Configure engines, containers, credentials and terms without the TUI."""
    ctx = ctx or load_context()
    gateway = ctx.envs["gateway"]
    deploy = ctx.envs["deploy"]
    _sync_engine_requirements(ctx)

    _title("newsvoice · configuración de engines (modo sin TUI)")
    print("  El asistente actualizará deploy/.env y gateway/.env; no toca news/.env.")

    engine_options = [*ENGINE_INFO, "routed"]
    current = gateway.get("TTS_ENGINE").strip() or "routed"
    index = (
        engine_options.index(current)
        if current in engine_options
        else engine_options.index("routed")
    )
    index = _choose("Engine principal", engine_options, index)
    gateway.set("TTS_ENGINE", engine_options[index])

    engine_field = _engine_field("TTS_ENGINES")
    if engine_field is not None:
        current = gateway.get(engine_field.key).strip() or engine_field.default
        answer = _read(f"  Mapa idioma→engine [{current}]: ")
        if answer:
            gateway.set(engine_field.key, answer)

    extra_field = _engine_field("TTS_EXTRA_ENGINES")
    if extra_field is not None:
        answer = _multi_prompt(extra_field, gateway.get(extra_field.key))
        if answer not in ("q", "s"):
            gateway.set(extra_field.key, answer)

    _set_required_models(ctx)
    if config_requires_gpu(
        gateway.get("TTS_ENGINE"),
        gateway.get("TTS_ENGINES"),
        gateway.get("TTS_EXTRA_ENGINES"),
    ):
        gateway.set("TTS_DEVICE", "cuda:0")
    _sync_gateway_docker_extras(ctx)
    _sync_engine_requirements(ctx)

    infos = selected_engine_infos(
        gateway.get("TTS_ENGINE"),
        gateway.get("TTS_ENGINES"),
        gateway.get("TTS_EXTRA_ENGINES"),
    )
    _title("Requisitos de la selección")
    if not infos:
        print("  Sin engines seleccionados.")
    for info in infos:
        needs = {"gateway": "gateway principal", "sidecar": "sidecar GPU"}.get(
            info.gpu_scope, "sin GPU"
        )
        services = ", ".join(info.services) or "sin contenedor"
        profiles = ", ".join(info.profiles) or "sin perfil"
        print(f"  {info.label}: {info.device} · {needs} · {services} · {profiles}")
        if not info.docker_supported:
            print(f"    ⚠ Docker no incluye este engine: {info.manual_install}")
    print(
        f"  GATEWAY_GPU={deploy.get('GATEWAY_GPU') or '0'} · COMPOSE_PROFILES="
        f"{deploy.get('COMPOSE_PROFILES') or '(vacío)'}"
    )

    keys: list[str] = []
    for info in infos:
        keys.extend(info.required_keys)
        keys.extend(info.optional_keys)
    required_keys = required_engine_keys(
        gateway.get("TTS_ENGINE"),
        gateway.get("TTS_ENGINES"),
        gateway.get("TTS_EXTRA_ENGINES"),
    )
    if keys:
        _title("Credenciales y API")
        for key in list(dict.fromkeys(keys)):
            field_ = _engine_field(key)
            if field_ is None:
                continue
            current = gateway.get(key).strip() or field_.default
            shown = _mask(current) if field_.kind in ("secret", "password") else current
            label = field_.label + (" (obligatoria)" if key in required_keys else " (opcional)")
            print(f"\n  {label}  ({field_.key})")
            print(f"    {field_.help}")
            answer = _read(f"    [{shown or 'vacío'}] ")
            if answer:
                gateway.set(key, answer)

    accepted = split_multi(deploy.get("ENGINE_TERMS_ACCEPTED"))
    terms = engine_terms(
        gateway.get("TTS_ENGINE"),
        gateway.get("TTS_ENGINES"),
        gateway.get("TTS_EXTRA_ENGINES"),
    )
    if terms:
        _title("Términos y licencias")
        for info in terms:
            print(f"  {info.label}: {info.terms_url or 'sin URL declarada'}")
            if info.key in accepted:
                print("    ya aceptado")
                continue
            if _confirm(
                f"¿Confirmas que aceptas los términos de {info.label}? [s/N]:",
                assume_yes=False,
            ):
                accepted.append(info.key)
    deploy.set("ENGINE_TERMS_ACCEPTED", join_multi(accepted))

    _sync_gateway_docker_extras(ctx)
    _sync_engine_requirements(ctx)
    _title("Cambios en deploy/.env y apps/gateway/.env")
    total = 0
    for key in ("deploy", "gateway"):
        changes = ctx.envs[key].diff_against_disk()
        if not changes:
            print(f"  {ctx.envs[key].key_path}: sin cambios")
            continue
        print(f"  {ctx.envs[key].key_path}:")
        for name, old, new in changes:
            print(f"    {name}: {_shown(name, old)} → {_shown(name, new)}")
        total += len(changes)
    if total == 0:
        print("  No hay nada que escribir.")

    saved = False
    if _confirm("¿Escribir deploy/.env y apps/gateway/.env? [s/N]:", assume_yes=assume_yes):
        saved = True
        for key in ("deploy", "gateway"):
            env = ctx.envs[key]
            if not env.changed_keys() and env.exists:
                continue
            backup = env.write()
            ctx.written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
        print(f"  Escrito: {', '.join(ctx.written) or 'nada'}")

    _title("Validación")
    checks = validation.validate(ctx.envs, ctx.report, will_write=saved, profile=ctx.profile.key)
    symbols = {"ok": "✓", "warn": "⚠", "bad": "✗"}
    for check in checks:
        print(f"  {symbols.get(check.status, '·')} {check.name}: {check.detail}")
    errors = sum(1 for check in checks if check.status == "bad")
    print(f"\n  {len(checks)} comprobaciones · {errors} error(es)")
    if saved:
        try:
            report_path = write_config_report(
                ctx.envs,
                ctx.report,
                profile=ctx.profile,
                method=ctx.method,
                checks=checks,
                written=ctx.written,
            )
        except OSError as exc:
            print(f"  ⚠ No se pudo escribir el informe de setup: {exc}")
        else:
            ctx.report_path = str(report_path)
            try:
                report_label = report_path.relative_to(REPO_ROOT)
            except ValueError:
                report_label = report_path
            print(f"  Informe sin secretos: {report_label}")
    print("  Siguientes pasos:  make up · make health · make engines")
    return 1 if errors else 0


def _finish(ctx: Context, *, write: bool, assume_yes: bool = False) -> int:
    """Install the dependencies and print the validation report.

    Args:
        ctx: Current wizard state.
        write: Whether the env files were written and the report should be generated.
        assume_yes: Answer every confirmation with yes.

    Returns:
        0 when validation found no errors, 1 otherwise.
    """
    _title("Paso 5 · dependencias")
    steps = _dependency_plan(ctx)
    if steps:
        for step in steps:
            print(f"  ▸ {step.title}: {' '.join(map(str, step.command))}")
        if _confirm("¿Ejecutar la instalación? [s/N]:", assume_yes=assume_yes):
            ok = deps.execute(
                steps, lambda status, title, detail: print(f"  [{status}] {title}: {detail}")
            )
            if ok and deps.verify(ctx.method):
                deps.execute(
                    deps.verify(ctx.method),
                    lambda status, title, detail: print(f"  [{status}] {title}: {detail}"),
                )
    else:
        print("  Se omite la instalación de dependencias.")

    _title("Paso 6 · validación")
    checks = validation.validate(ctx.envs, ctx.report, will_write=write, profile=ctx.profile.key)
    symbols = {"ok": "✓", "warn": "⚠", "bad": "✗"}
    for check in checks:
        print(f"  {symbols.get(check.status, '·')} {check.name}: {check.detail}")
    errors = sum(1 for check in checks if check.status == "bad")
    print(f"\n  {len(checks)} comprobaciones · {errors} error(es)")
    if write:
        try:
            report_path = write_config_report(
                ctx.envs,
                ctx.report,
                profile=ctx.profile,
                method=ctx.method,
                checks=checks,
                written=ctx.written,
            )
        except OSError as exc:
            print(f"  ⚠ No se pudo escribir el informe de setup: {exc}")
        else:
            ctx.report_path = str(report_path)
            try:
                report_label = report_path.relative_to(REPO_ROOT)
            except ValueError:
                report_label = report_path
            print(f"  Informe sin secretos: {report_label}")
    if ctx.written:
        print("  Siguientes pasos:  make up · make status · make urls")
    return 1 if errors else 0
