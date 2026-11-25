"""Automatic repairs shared by the TUI, the line mode and the CLI."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine_runtime import EngineRuntimeError

from .settings import (
    CLOUD_ENGINES,
    MODEL_OPTIONS,
    ROUTED_PRESETS,
    canonical_engine,
    config_requires_gpu,
    engine_mode,
    engine_modes,
    gateway_docker_extras,
    join_multi,
    managed_compose_profiles,
    parse_engine_map,
    parse_runtime_modes,
    render_engine_map,
    render_runtime_modes,
    required_model_downloads,
    runtime_plan_for,
    runtime_specs,
    split_multi,
    valid_language_code,
)
from .util import generate_secret

if TYPE_CHECKING:
    from .app import Context

__all__ = [
    "Fix",
    "RepairResult",
    "all_fixes",
    "apply_fixes",
    "conflict_fixes",
    "manual_issues",
    "shared_key_conflicts",
    "shared_key_source",
    "shared_key_values",
    "unify_shared_key",
]

_KEY_LINE = re.compile(r"^\s*(#)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_PAIR = re.compile(r"^([^+]+)\+([^+]+) no pueden")
_SHARED_KEYS = (
    ("deploy", "GATEWAY_API_KEY"),
    ("news", "TTS_API_KEY"),
    ("gateway", "API_KEY"),
)
_INTERNAL_SECRETS = (
    ("news", "ADMIN_API_TOKEN"),
    ("news", "DOC_TTS_LINK_SECRET"),
)


@dataclass
class Fix:
    """One staged repair action.

    Attributes:
        id: Stable identifier used by callers to avoid duplicate actions.
        label: Short description shown in the UI.
        detail: Explanation of the change, without secret values.
        apply: Callback that stages the change in memory.
    """

    id: str
    label: str
    detail: str
    apply: Callable[[Context], str] = field(repr=False)


@dataclass
class RepairResult:
    """Outcome of one repair pass.

    Attributes:
        applied: Labels of the fixes that were staged.
        manual: Problems that still need operator action.
        error: First error raised by a fix, if any.
    """

    applied: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    error: str = ""


def _gateway(ctx: Context):
    return ctx.envs.get("gateway")


def _selection(ctx: Context) -> tuple[str, dict[str, str], list[str]]:
    gateway = _gateway(ctx)
    if gateway is None:
        return "", {}, []
    engine = gateway.get("TTS_ENGINE").strip()
    routing = parse_engine_map(gateway.get("TTS_ENGINES")) or {}
    extra = split_multi(gateway.get("TTS_EXTRA_ENGINES"))
    return engine, routing, extra


def _modes(ctx: Context) -> dict[str, str]:
    gateway = _gateway(ctx)
    return parse_runtime_modes(gateway.get("TTS_RUNTIME_MODES")) if gateway else {}


def _valid_ids() -> set[str]:
    return {
        name for name, spec in runtime_specs().items() if spec.selectable and spec.gateway_engine
    }


def _selected(engine: str, routing: dict[str, str], extra: list[str]) -> list[str]:
    names = [engine] if engine and engine != "routed" else []
    names.extend(routing.values())
    names.extend(extra)
    return list(dict.fromkeys(names))


def _write_selection(
    ctx: Context,
    engine: str,
    routing: dict[str, str],
    extra: list[str],
    modes: dict[str, str],
) -> None:
    gateway = _gateway(ctx)
    if gateway is None:
        return
    gateway.set("TTS_ENGINE", engine)
    gateway.set("TTS_ENGINES", render_engine_map(routing))
    gateway.set("TTS_EXTRA_ENGINES", join_multi(extra))
    gateway.set("TTS_RUNTIME_MODES", render_runtime_modes(modes))
    plan = runtime_plan_for(
        engine, gateway.get("TTS_ENGINES"), gateway.get("TTS_EXTRA_ENGINES"), modes
    )
    if plan.conflicts:
        raise EngineRuntimeError(" · ".join(plan.conflicts))
    _sync_derived(ctx)


def _sync_derived(ctx: Context) -> None:
    from . import app

    app._sync_gateway_docker_extras(ctx)
    app._sync_engine_requirements(ctx)


def _capture(ctx: Context) -> dict[str, tuple[list[str], dict[str, str], dict[str, str]]]:
    return {
        key: (list(env.lines), dict(env.edits), dict(env.pristine)) for key, env in ctx.envs.items()
    }


def _restore(
    ctx: Context,
    snapshot: dict[str, tuple[list[str], dict[str, str], dict[str, str]]],
) -> None:
    for key, (lines, edits, pristine) in snapshot.items():
        env = ctx.envs.get(key)
        if env is None:
            continue
        env.lines[:] = lines
        env.edits.clear()
        env.edits.update(edits)
        env.pristine.clear()
        env.pristine.update(pristine)


def _dedupe(fixes: list[Fix]) -> list[Fix]:
    seen: set[str] = set()
    result: list[Fix] = []
    for fix in fixes:
        if fix.id in seen:
            continue
        seen.add(fix.id)
        result.append(fix)
    return result


def shared_key_values(ctx: Context) -> dict[tuple[str, str], str]:
    """Return the current value of the gateway key in each file."""
    return {
        (file_key, key): ctx.envs[file_key].get(key).strip()
        for file_key, key in _SHARED_KEYS
        if file_key in ctx.envs
    }


def shared_key_source(ctx: Context) -> str:
    """Return the value every file should hold, with deploy taking precedence."""
    values = shared_key_values(ctx)
    deploy = values.get(("deploy", "GATEWAY_API_KEY"))
    return deploy or next((value for value in values.values() if value), "")


def shared_key_conflicts(ctx: Context) -> list[str]:
    """Return ``file:key`` labels whose gateway key differs from the source."""
    source = shared_key_source(ctx)
    if not source:
        return []
    return [
        f"{file_key}/{key}"
        for (file_key, key), value in shared_key_values(ctx).items()
        if value and value != source
    ]


def unify_shared_key(ctx: Context) -> int:
    """Write the source gateway key into every file and return the change count."""
    source = shared_key_source(ctx)
    if not source:
        return 0
    changed = 0
    for (file_key, key), value in shared_key_values(ctx).items():
        if value != source:
            ctx.envs[file_key].set(key, source)
            changed += 1
    return changed


def _duplicate_fixes(ctx: Context) -> list[Fix]:
    fixes: list[Fix] = []
    for file_key, env in ctx.envs.items():
        seen: set[str] = set()
        duplicates: list[int] = []
        for index, line in enumerate(env.lines):
            match = _KEY_LINE.match(line)
            if match is None or match.group(1) or not match.group(2):
                continue
            key = match.group(2)
            if key in seen:
                duplicates.append(index)
            seen.add(key)
        if not duplicates:
            continue

        def apply_duplicates(
            context: Context,
            *,
            target: str = file_key,
            rows: tuple[int, ...] = tuple(duplicates),
        ) -> str:
            current = context.envs[target]
            for index in rows:
                line = current.lines[index]
                indent = re.match(r"^\s*", line).group(0)
                current.lines[index] = f"{indent}# {line.lstrip()}"
            return f"{target}/.env: {len(rows)} clave(s) duplicada(s) comentada(s)"

        fixes.append(
            Fix(
                f"duplicates:{file_key}",
                f"Claves duplicadas en {file_key}/.env",
                f"{len(duplicates)} repetición(es) comentada(s); gana la primera activa",
                apply_duplicates,
            )
        )
    return fixes


def _canonical_fix(ctx: Context) -> Fix | None:
    engine, routing, extra = _selection(ctx)
    modes = _modes(ctx)
    new_engine = canonical_engine(engine) if engine and engine != "routed" else engine
    new_routing = {code: canonical_engine(name) for code, name in routing.items()}
    new_extra = [canonical_engine(name) for name in extra]
    new_modes = {canonical_engine(name): mode for name, mode in modes.items()}
    if (new_engine, new_routing, new_extra, new_modes) == (engine, routing, extra, modes):
        return None
    count = sum(
        (
            new_engine != engine,
            new_routing != routing,
            new_extra != extra,
            new_modes != modes,
        )
    )

    def apply_canonical(context: Context) -> str:
        _write_selection(context, new_engine, new_routing, new_extra, new_modes)
        return f"{count} bloque(s) canonicalizado(s)"

    return Fix(
        "canonical",
        "Canonicalizar IDs de engines",
        "Alias y mayúsculas se sustituyen por IDs canónicos en selección, mapa y modos",
        apply_canonical,
    )


def _invalid_fix(ctx: Context) -> Fix | None:
    engine, routing, extra = _selection(ctx)
    modes = _modes(ctx)
    valid = _valid_ids()
    new_engine = engine if engine == "routed" or engine in valid else ""
    new_routing = {code: name for code, name in routing.items() if name in valid}
    new_extra = [name for name in extra if name in valid]
    selected = _selected(new_engine, new_routing, new_extra)
    new_modes = {name: mode for name, mode in modes.items() if name in valid and name in selected}
    if not new_engine and (new_routing or new_extra):
        new_engine = "routed"
    if not new_engine and valid:
        new_engine = next(
            (name for name in ("edgetts", "pocket_tts") if name in valid), sorted(valid)[0]
        )
    if (new_engine, new_routing, new_extra, new_modes) == (engine, routing, extra, modes):
        return None

    def apply_invalid(context: Context) -> str:
        _write_selection(context, new_engine, new_routing, new_extra, new_modes)
        return "selección limitada a engines configurables del gateway"

    return Fix(
        "invalid-ids",
        "Retirar engines no configurables",
        "Se descartan IDs desconocidos, no seleccionables y proxies sin gateway",
        apply_invalid,
    )


def _routing_fix(ctx: Context) -> Fix | None:
    engine, routing, extra = _selection(ctx)
    modes = _modes(ctx)
    valid = _valid_ids()
    if engine == "routed":
        new_routing = {
            code: name
            for code, name in routing.items()
            if name in valid and (code == "_default" or valid_language_code(code))
        }
        if not new_routing:
            preset = ROUTED_PRESETS[0]
            new_routing = parse_engine_map(preset.routing) or {}
            extra = split_multi(preset.extra)
        new_routing.setdefault("_default", next(iter(new_routing.values()), ""))
    elif extra:
        new_routing = {"_default": engine} if engine else {}
        engine = "routed"
    else:
        return None
    if (engine, new_routing, extra) == (_selection(ctx)[0], _selection(ctx)[1], _selection(ctx)[2]):
        return None

    def apply_routing(context: Context) -> str:
        _write_selection(context, engine, new_routing, extra, modes)
        return "mapa routed reparado con _default y destinos válidos"

    return Fix(
        "routing",
        "Reparar mapa idiomático",
        "Se corrige JSON, idiomas y destinos, y se garantiza _default",
        apply_routing,
    )


def _alternative(name: str, current: str) -> str:
    options = [mode for mode in engine_modes(name) if mode not in ("embedded", current)]
    options = [mode for mode in options if mode != "disabled"] or ["disabled"]
    preferred = engine_mode(name)
    if preferred in options and preferred != "disabled":
        return preferred
    for candidate in ("sidecar", "modal"):
        if candidate in options:
            return candidate
    return options[0]


def _conflict_fixes(ctx: Context) -> list[Fix]:
    engine, routing, extra = _selection(ctx)
    modes = _modes(ctx)
    try:
        plan = runtime_plan_for(engine, render_engine_map(routing), join_multi(extra), modes)
    except EngineRuntimeError:
        return []
    if not plan.conflicts:
        return []
    match = _PAIR.match(plan.conflicts[0])
    if match is None:
        return []
    first, second = canonical_engine(match.group(1)), canonical_engine(match.group(2))
    candidates = [name for name in (first, second) if name in _selected(engine, routing, extra)]
    if not candidates:
        return []
    target = candidates[0]
    alternative = _alternative(target, modes.get(target, engine_mode(target)))
    new_modes = dict(modes)
    new_routing = dict(routing)
    new_extra = list(extra)
    new_engine = engine
    if alternative == "disabled":
        new_modes.pop(target, None)
        new_routing = {code: name for code, name in routing.items() if name != target}
        new_extra = [name for name in extra if name != target]
        if new_engine == target:
            new_engine = "routed" if new_routing or new_extra else ""
        if new_engine == "routed" and not new_routing and new_extra:
            new_engine = new_extra.pop(0)
        if new_engine == "routed" and new_routing:
            new_routing.setdefault("_default", next(iter(new_routing.values())))
    else:
        new_modes[target] = alternative

    def apply_conflict(context: Context) -> str:
        _write_selection(context, new_engine, new_routing, new_extra, new_modes)
        label = "desactivado" if alternative == "disabled" else alternative
        return f"{target} → {label}; conflicto {first}+{second} resuelto"

    return [
        Fix(
            f"conflict:{first}+{second}",
            f"Resolver conflicto {first}+{second}",
            f"Cambiar {target} a {alternative} para no compartir el gateway",
            apply_conflict,
        )
    ]


def _derived_fix(ctx: Context) -> Fix | None:
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = _modes(ctx)
    try:
        plan = runtime_plan_for(engine, engine_map, extra, modes)
    except EngineRuntimeError:
        return None
    deploy = ctx.envs.get("deploy")
    expected_extras = gateway_docker_extras(engine, engine_map, extra, modes)
    expected_profiles = ",".join(sorted(plan.profiles))
    expected_gpu = "1" if config_requires_gpu(engine, engine_map, extra, modes) else "0"
    gateway_env = _gateway(ctx)
    drift = (
        gateway_env.get("TTS_RUNTIME_PROFILES").strip() != expected_profiles
        if gateway_env
        else False
    )
    if deploy is not None:
        expected_profiles_text = managed_compose_profiles(
            deploy.get("COMPOSE_PROFILES"), engine, engine_map, extra, plan.profiles
        )
        drift = drift or deploy.get("COMPOSE_PROFILES").strip() != expected_profiles_text
        drift = drift or deploy.get("GATEWAY_UV_EXTRAS").strip() != expected_extras
        drift = drift or deploy.get("GATEWAY_GPU").strip() != expected_gpu
    if not drift:
        return None

    def apply_derived(context: Context) -> str:
        _sync_derived(context)
        return "extras, perfiles, GPU y URLs derivados de la selección actual"

    return Fix(
        "derived",
        "Sincronizar infraestructura derivada",
        "Recalcula Docker extras, perfiles Compose, GPU y URLs de sidecar",
        apply_derived,
    )


def _gateway_selection(ctx: Context) -> tuple[str, str, str]:
    gateway = _gateway(ctx)
    if gateway is None:
        return "", "", ""
    return (
        gateway.get("TTS_ENGINE").strip(),
        gateway.get("TTS_ENGINES").strip(),
        gateway.get("TTS_EXTRA_ENGINES").strip(),
    )


def _secret_fix(ctx: Context) -> Fix | None:
    conflicts = shared_key_conflicts(ctx)
    values = shared_key_values(ctx)
    missing_key = not any(values.values())
    missing_internal = [
        f"{file_key}.{key}"
        for file_key, key in _INTERNAL_SECRETS
        if file_key in ctx.envs and not ctx.envs[file_key].get(key).strip()
    ]
    if not conflicts and not missing_key and not missing_internal:
        return None
    detail_parts: list[str] = []
    if missing_key:
        detail_parts.append("se genera la clave interna del gateway")
    if conflicts:
        detail_parts.append("se unifica con el valor de deploy/.env")
    if missing_internal:
        detail_parts.append(f"se generan {len(missing_internal)} secreto(s) internos")

    def apply_secrets(context: Context) -> str:
        if missing_key or conflicts:
            source = shared_key_source(context)
            if not source:
                source = generate_secret()
                context.envs["deploy"].set("GATEWAY_API_KEY", source)
            for (file_key, key), value in shared_key_values(context).items():
                if value != source:
                    context.envs[file_key].set(key, source)
        for file_key, key in _INTERNAL_SECRETS:
            env = context.envs.get(file_key)
            if env is not None and not env.get(key).strip():
                env.set(key, generate_secret())
        return "secretos internos y clave compartida alineados"

    return Fix("secrets", "Reparar secretos internos", " · ".join(detail_parts), apply_secrets)


def _device_fix(ctx: Context) -> Fix | None:
    gateway = _gateway(ctx)
    if gateway is None:
        return None
    engine, engine_map, extra = _gateway_selection(ctx)
    current = gateway.get("TTS_DEVICE").strip()
    required = config_requires_gpu(engine, engine_map, extra, _modes(ctx))
    target = "cuda:0" if required else "cpu"
    if current == target or (not current and required):
        return None

    def apply_device(context: Context) -> str:
        target_env = context.envs["gateway"]
        target_env.set("TTS_DEVICE", target)
        _sync_derived(context)
        return f"TTS_DEVICE={target}"

    return Fix(
        "device",
        "Ajustar dispositivo TTS",
        f"La selección requiere {'GPU' if required else 'CPU'}; se usa {target}",
        apply_device,
    )


def _models_fix(ctx: Context) -> Fix | None:
    gateway = _gateway(ctx)
    if gateway is None:
        return None
    engine, engine_map, extra = _gateway_selection(ctx)
    modes = _modes(ctx)
    current = split_multi(gateway.get("TTS_MODEL_DOWNLOADS"))
    required = required_model_downloads(engine, engine_map, extra, modes)
    keep = [
        name
        for name in current
        if name in MODEL_OPTIONS and name.split(":", 1)[0] not in CLOUD_ENGINES
    ]
    target = join_multi([*required, *keep])
    if target == join_multi(current):
        return None

    def apply_models(context: Context) -> str:
        context.envs["gateway"].set("TTS_MODEL_DOWNLOADS", target)
        return f"{len(split_multi(target))} modelo(s) de descarga alineados"

    return Fix(
        "models",
        "Sincronizar modelos",
        "Descarta modelos desconocidos o cloud y añade los checkpoints obligatorios",
        apply_models,
    )


def conflict_fixes(ctx: Context) -> list[Fix]:
    """Return staged repairs for engine selection and runtime conflicts."""
    fixes = [
        *_duplicate_fixes(ctx),
        *([fix] if (fix := _canonical_fix(ctx)) else []),
        *([fix] if (fix := _invalid_fix(ctx)) else []),
        *([fix] if (fix := _routing_fix(ctx)) else []),
        *_conflict_fixes(ctx),
        *([fix] if (fix := _derived_fix(ctx)) else []),
    ]
    return _dedupe(fixes)


def all_fixes(ctx: Context) -> list[Fix]:
    """Return every repair that can be staged without touching the host."""
    fixes = [
        *conflict_fixes(ctx),
        *([fix] if (fix := _secret_fix(ctx)) else []),
        *([fix] if (fix := _device_fix(ctx)) else []),
        *([fix] if (fix := _models_fix(ctx)) else []),
    ]
    return _dedupe(fixes)


def apply_fixes(ctx: Context, fixes: list[Fix]) -> RepairResult:
    """Apply staged fixes, rolling back each one that raises an error."""
    result = RepairResult()
    for fix in fixes:
        snapshot = _capture(ctx)
        try:
            detail = fix.apply(ctx)
        except Exception as exc:
            _restore(ctx, snapshot)
            result.error = f"{fix.label}: {exc}"
            break
        result.applied.append(f"{fix.label}: {detail}")
    result.manual = manual_issues(ctx)
    return result


def manual_issues(ctx: Context) -> list[str]:
    """Return validation problems that cannot be repaired automatically."""
    from .validate import validate

    auto = {
        "claves duplicadas",
        "tts engine",
        "engines extra",
        "modos de carga",
        "routed",
        "conflictos de runtime",
        "engines configurables",
        "docker extras",
        "perfiles engines",
        "perfiles de runtime",
        "urls de sidecar",
        "tts device",
        "gpu overlay",
        "modelos",
        "clave compartida",
        "acceso al panel",
        "enlaces firmados",
    }
    checks = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    return [
        f"{check.name}: {check.detail}"
        for check in checks
        if check.status in ("bad", "warn") and check.name not in auto
    ]
