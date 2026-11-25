"""Planificador de modos de carga para los engines TTS canónicos."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = ROOT / "config" / "engine_runtimes.toml"
CONTRACT_PATH = ROOT / "libs" / "tts_contract" / "tts_contract" / "engines.py"
MODES = frozenset({"embedded", "sidecar", "modal", "disabled"})


class EngineRuntimeError(ValueError):
    """Error de configuración del registro de engines."""


@dataclass(frozen=True)
class RuntimeSpec:
    """Modo de carga y recursos de un engine canónico.

    Attributes:
        engine: Identificador canónico.
        mode: Modo por defecto.
        modes: Modos soportados.
        service: Servicio Docker que ejecuta el engine, si aplica.
        profile: Profile de compose requerido, si aplica.
        dockerfile: Dockerfile del sidecar, si aplica.
        url: URL interna por defecto del sidecar.
        url_env: Variable de entorno que apunta al sidecar.
        health_path: Ruta de health del sidecar.
        gpu: Si el engine necesita GPU.
        pip_extra: Extra de `uv` necesario en la imagen gateway.
        modal_function: Nombre de la función Modal, si aplica.
        incompatible_with: Engines que no pueden compartir el mismo entorno.
        requires_service: Servicio auxiliar necesario cuando el engine es embebido.
        gateway_engine: Si el catálogo del gateway conoce este ID.
        selectable: Si puede aparecer en la selección TTS_ENGINE.
    """

    engine: str
    mode: str
    modes: tuple[str, ...]
    service: str = ""
    profile: str = ""
    dockerfile: str = ""
    url: str = ""
    url_env: str = ""
    health_path: str = "/api/v1/health"
    gpu: bool = False
    pip_extra: str = ""
    modal_function: str = ""
    incompatible_with: tuple[str, ...] = ()
    requires_service: str = ""
    gateway_engine: bool = True
    selectable: bool = True


@dataclass
class RuntimePlan:
    """Resultado de resolver una selección de engines.

    Attributes:
        engines: Engines canónicos activos, en orden de selección.
        gateway_engines: Engines activos que el gateway puede instanciar.
        modes: Modo efectivo por engine.
        services: Servicios Docker a arrancar.
        profiles: Profiles de compose a habilitar.
        dockerfiles: Dockerfiles de sidecar implicados.
        urls: Variables de entorno y valores de URL a exportar.
        extras: Extras de `uv` necesarios en la imagen gateway.
        modal_functions: Funciones Modal implicadas.
        gpu: Si hay que añadir el overlay GPU.
        conflicts: Incompatibilidades detectadas.
    """

    engines: tuple[str, ...] = ()
    gateway_engines: tuple[str, ...] = ()
    modes: dict[str, str] = field(default_factory=dict)
    services: set[str] = field(default_factory=set)
    profiles: set[str] = field(default_factory=set)
    dockerfiles: set[str] = field(default_factory=set)
    urls: dict[str, str] = field(default_factory=dict)
    extras: set[str] = field(default_factory=set)
    modal_functions: set[str] = field(default_factory=set)
    gpu: bool = False
    conflicts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serializar el plan para el wrapper de shell y los tests."""
        return {
            "engines": list(self.engines),
            "gateway_engines": list(self.gateway_engines),
            "modes": dict(sorted(self.modes.items())),
            "services": sorted(self.services),
            "profiles": sorted(self.profiles),
            "dockerfiles": sorted(self.dockerfiles),
            "urls": dict(sorted(self.urls.items())),
            "extras": sorted(self.extras),
            "modal_functions": sorted(self.modal_functions),
            "gpu": self.gpu,
            "conflicts": list(self.conflicts),
        }


def _contract_constant(name: str) -> Any:
    tree = ast.parse(CONTRACT_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        target: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1:
                target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise EngineRuntimeError(f"{name} no encontrado en {CONTRACT_PATH}")


def load_contract() -> tuple[tuple[str, ...], dict[str, str]]:
    """Leer IDs canónicos y aliases del contrato compartido."""
    canonical = tuple(_contract_constant("CANONICAL_ENGINE_IDS"))
    aliases = dict(_contract_constant("LEGACY_ENGINE_ALIASES"))
    return canonical, aliases


def canonicalize_engine(
    value: str, aliases: Mapping[str, str], canonical: Iterable[str] | None = None
) -> str:
    """Normalizar un ID y rechazar aliases ambiguos."""
    raw = value.strip().lower()
    result = aliases.get(raw, raw)
    valid = set(canonical if canonical is not None else aliases.values())
    if result not in valid:
        raise EngineRuntimeError(f"engine desconocido: {value!r}")
    return result


def load_runtimes(path: Path | None = None) -> dict[str, RuntimeSpec]:
    """Cargar y validar el registro de modos de carga."""
    source = path or RUNTIME_PATH
    with source.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("version") != 1:
        raise EngineRuntimeError(f"versión de runtime no soportada en {source}")
    defaults = data.get("defaults", {})
    default_health = str(defaults.get("health_path", "/api/v1/health"))
    canonical, aliases = load_contract()
    expected = set(canonical) - {"mock"}
    found = set(data.get("engines", {}))
    if found != expected:
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        raise EngineRuntimeError(
            f"registro de runtimes inconsistente; faltan {missing} y hay IDs extra {extra}"
        )
    specs: dict[str, RuntimeSpec] = {}
    for engine, raw in data["engines"].items():
        mode = str(raw.get("mode", "embedded"))
        modes = tuple(str(item) for item in raw.get("modes", (mode,)))
        if mode not in MODES or not set(modes) <= MODES:
            raise EngineRuntimeError(f"modo inválido para {engine}: {mode}/{modes}")
        specs[engine] = RuntimeSpec(
            engine=engine,
            mode=mode,
            modes=modes,
            service=str(raw.get("service", aliases.get(engine, engine))),
            profile=str(raw.get("profile", "")),
            dockerfile=str(raw.get("dockerfile", "")),
            url=str(raw.get("url", "")),
            url_env=str(raw.get("url_env", "")),
            health_path=str(raw.get("health_path", default_health)),
            gpu=bool(raw.get("gpu", False)),
            pip_extra=str(raw.get("pip_extra", "")),
            modal_function=str(raw.get("modal_function", "")),
            incompatible_with=tuple(str(item) for item in raw.get("incompatible_with", ())),
            requires_service=str(raw.get("requires_service", "")),
            gateway_engine=bool(raw.get("gateway_engine", True)),
            selectable=bool(raw.get("selectable", True)),
        )
    return specs


def resolve_plan(
    engines: Iterable[str],
    mode_overrides: Mapping[str, str] | None = None,
    specs: Mapping[str, RuntimeSpec] | None = None,
) -> RuntimePlan:
    """Resolver engines, modos, servicios y conflictos de una selección."""
    registry = specs or load_runtimes()
    canonical, aliases = load_contract()
    overrides = dict(mode_overrides or {})
    plan = RuntimePlan()
    requested: list[tuple[str, RuntimeSpec, str]] = []
    for raw in engines:
        engine = canonicalize_engine(raw, aliases, canonical)
        if engine not in registry:
            raise EngineRuntimeError(f"engine sin runtime registrado: {engine}")
        spec = registry[engine]
        mode = overrides.pop(engine, spec.mode)
        if mode not in spec.modes:
            raise EngineRuntimeError(
                f"modo {mode!r} no soportado por {engine}; usa {', '.join(spec.modes)}"
            )
        if mode == "disabled":
            continue
        requested.append((engine, spec, mode))
    if overrides:
        unknown = ", ".join(sorted(overrides))
        raise EngineRuntimeError(f"override de modo para engines no seleccionados: {unknown}")
    plan.engines = tuple(engine for engine, _, _ in requested)
    plan.gateway_engines = tuple(engine for engine, spec, _ in requested if spec.gateway_engine)
    active = {engine: mode for engine, _, mode in requested}
    plan.modes = active
    for engine, spec, mode in requested:
        plan.gpu = plan.gpu or spec.gpu
        if mode == "sidecar":
            if not spec.service:
                raise EngineRuntimeError(f"{engine} no define servicio sidecar")
            plan.services.add(spec.service)
            if spec.profile:
                plan.profiles.add(spec.profile)
            if spec.dockerfile:
                plan.dockerfiles.add(spec.dockerfile)
            if spec.url_env and spec.url:
                plan.urls[spec.url_env] = spec.url
        elif mode == "modal":
            plan.profiles.add("modal")
            if spec.modal_function:
                plan.modal_functions.add(spec.modal_function)
        elif mode == "embedded":
            if spec.pip_extra:
                plan.extras.add(spec.pip_extra)
            if spec.requires_service:
                plan.services.add(spec.requires_service)
                plan.profiles.add("omnivoice" if "omnivoice" in spec.requires_service else "")
                plan.urls.setdefault("OMNIVOICE_REMOTE_URL", "http://gateway-omnivoice:8000")
    plan.profiles.discard("")
    for engine, spec, mode in requested:
        if mode != "embedded":
            continue
        for other in spec.incompatible_with:
            if other in active and active[other] == "embedded":
                pair = f"{engine}+{other}"
                message = (
                    f"{pair} no pueden compartir el entorno gateway; "
                    f"usa --mode {other}=sidecar o --mode {engine}=modal"
                )
                if message not in plan.conflicts:
                    plan.conflicts.append(message)
    return plan


def env_updates(plan: RuntimePlan) -> dict[str, str]:
    """Devolver las variables que el loader debe persistir."""
    selectable = list(plan.gateway_engines)
    updates = {
        "TTS_ENGINE": "routed" if len(selectable) != 1 else selectable[0],
        "TTS_EXTRA_ENGINES": ",".join(selectable),
        "TTS_RUNTIME_MODES": json.dumps(plan.modes, sort_keys=True, separators=(",", ":")),
        "TTS_RUNTIME_PROFILES": ",".join(sorted(plan.profiles)),
    }
    updates.update(plan.urls)
    return updates


def write_env(
    updates: Mapping[str, str], root: Path | None = None, dry_run: bool = False
) -> dict[str, list[str]]:
    """Escribir los tres `.env` existentes preservando comentarios."""
    from setup_wizard.envfile import EnvFile

    base = root or ROOT
    targets = (
        (base / "deploy" / ".env", base / "deploy" / ".env.example"),
        (base / "apps" / "gateway" / ".env", base / "apps" / "gateway" / ".env.example"),
        (base / "apps" / "news" / ".env", base / "apps" / "news" / ".env.example"),
    )
    changes: dict[str, list[str]] = {}
    for path, example in targets:
        env = EnvFile.load(path, example)
        for key, value in updates.items():
            env.set(key, value)
        changed = env.diff_against_disk()
        changes[str(path.relative_to(base))] = [
            f"{key}: {old!r} -> {new!r}" for key, old, new in changed
        ]
        if changed and not dry_run:
            env.write(keep_backup=False)
    return changes


def _parse_mode(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise EngineRuntimeError(f"override de modo inválido: {value!r}; usa ENGINE=MODE")
    engine, mode = value.split("=", 1)
    return engine.strip().lower(), mode.strip().lower()


def main(argv: list[str] | None = None) -> int:
    """CLI del planificador de runtimes."""
    parser = argparse.ArgumentParser(description="Planifica la carga de engines TTS")
    parser.add_argument("--engine", action="append", default=[], help="ID canónico (repetible)")
    parser.add_argument("--mode", action="append", default=[], help="ENGINE=MODE (repetible)")
    parser.add_argument("--registry", type=Path, default=RUNTIME_PATH)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true", help="no escribe archivos .env")
    args = parser.parse_args(argv)
    try:
        specs = load_runtimes(args.registry)
        overrides = dict(_parse_mode(item) for item in args.mode)
        plan = resolve_plan(args.engine, overrides, specs)
        updates = env_updates(plan)
        if not args.dry_run:
            write_env(updates, args.root, dry_run=False)
        result = plan.as_dict()
        result["env"] = updates
        result["dry_run"] = bool(args.dry_run)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (EngineRuntimeError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
