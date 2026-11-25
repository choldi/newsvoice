"""Command line entry point: ``python3 scripts/setup.py`` or ``-m setup_wizard``."""

from __future__ import annotations

import argparse
import sys

from . import __version__, repair
from .app import _record_repair, build_app, load_context
from .config_report import write_config_report
from .plain import _show_repair_result
from .settings import PROFILES
from .validate import validate

__all__ = ["main"]

_SYMBOLS = {"ok": "✓", "warn": "⚠", "bad": "✗", "info": "·"}


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        Configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Instalador guiado de newsvoice: configura los tres .env y las dependencias.",
        epilog="Sin terminal interactiva se usa el modo línea automáticamente.",
    )
    parser.add_argument("--version", action="version", version=f"newsvoice setup {__version__}")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Fuerza el modo línea aunque haya terminal (útil para redirigir la salida)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Solo detecta el entorno y valida los .env, sin escribir nada",
    )
    fixes = parser.add_mutually_exclusive_group()
    fixes.add_argument(
        "--fix-conflicts",
        action="store_true",
        help="Repara conflictos de engines y modos, y escribe los .env",
    )
    fixes.add_argument(
        "--fix-all",
        action="store_true",
        help="Repara todos los problemas automatizables y escribe los .env",
    )
    fixes.add_argument(
        "--ai-fix",
        action="store_true",
        help="Descarga un agente efímero, diagnostica y aplica correcciones con confirmación",
    )
    parser.add_argument(
        "--ai-fix-dry-run",
        action="store_true",
        help="Con --ai-fix: muestra el diagnóstico y las propuestas sin aplicarlas",
    )
    parser.add_argument(
        "--ai-fix-with-tests",
        action="store_true",
        help="Con --ai-fix: incluye pytest y ruff en la evidencia",
    )
    parser.add_argument(
        "--engines",
        action="store_true",
        help="Abre directamente el asistente de engines TTS (contenedores, GPU, API y términos)",
    )
    parser.add_argument(
        "--profile",
        choices=[profile.key for profile in PROFILES],
        default="",
        help="Punto de partida (omite el paso 1)",
    )
    parser.add_argument(
        "--install",
        choices=["uv", "venv", "skip"],
        default="",
        help="Método de instalación de dependencias (omite el paso 2)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Responde «sí» a todas las confirmaciones del modo línea",
    )
    return parser


def _print_report(report) -> None:
    """Print the host detection as a plain list.

    Args:
        report: Detection report.
    """
    from .detect import prerequisites

    print(f"Python {report.python}")
    for finding in report.findings:
        print(f"  {_SYMBOLS.get(finding.status, '·')} {finding.name}: {finding.detail}")
    for port, busy in report.ports.items():
        if busy:
            print(f"  ⚠ puerto {port}: ya está en uso")
    pending = [item for item in prerequisites(report) if item.missing]
    if pending:
        print("requisitos pendientes (qué falta y cómo instalarlo):")
        for item in pending:
            print(f"  {item.symbol} {item.name}: {item.why}")
            for command in item.remedy:
                print(f"      $ {command}")
            if not item.remedy:
                print("      (instalación manual)")


def _check(include_detect: bool = True, profile: str = "") -> int:
    """Run the read-only inspection and validation.

    Args:
        include_detect: Print the host report as well.
        profile: Profile whose assumptions the rules should apply.

    Returns:
        1 when a rule failed, 0 otherwise.
    """
    ctx = load_context()
    if profile:
        ctx.profile = next((p for p in PROFILES if p.key == profile), ctx.profile)
    if include_detect:
        print("== entorno ==")
        _print_report(ctx.report)
    print("\n== validación ==")
    checks = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    for check in checks:
        print(f"  {_SYMBOLS.get(check.status, '·')} {check.name}: {check.detail}")
    errors = sum(1 for check in checks if check.status == "bad")
    warns = sum(1 for check in checks if check.status == "warn")
    print(f"\n{len(checks)} comprobaciones · {errors} error(es) · {warns} aviso(s)")
    return 1 if errors else 0


def _run_fix(ctx, *, conflicts_only: bool, assume_yes: bool) -> int:
    """Stage repairs, write the env files and report the remaining problems."""
    fixes = repair.conflict_fixes(ctx) if conflicts_only else repair.all_fixes(ctx)
    before = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    before_bad = sum(1 for check in before if check.status == "bad")
    before_warn = sum(1 for check in before if check.status == "warn")
    print("== reparación ==")
    print(f"  antes: {before_bad} error(es) · {before_warn} aviso(s)")
    if not fixes:
        print("  No hay reparaciones pendientes.")
        return 0
    for fix in fixes:
        print(f"  · {fix.label}: {fix.detail}")
    answer = "s" if assume_yes else input("  ¿Aplicar las reparaciones? [s/N]: ").strip().lower()
    if answer not in ("s", "sí", "si", "y", "yes"):
        print("  Reparación cancelada; no se escribió ningún archivo.")
        return 0
    result = repair.apply_fixes(ctx, fixes)
    _record_repair(ctx, result)
    _show_repair_result(result)
    if result.error:
        return 1
    written: list[str] = []
    for key in ("deploy", "news", "gateway"):
        env = ctx.envs[key]
        if not env.changed_keys() and env.exists:
            continue
        backup = env.write()
        written.append(env.key_path + (f" (copia: {backup.name})" if backup else ""))
    print(f"  Escrito: {', '.join(written) or 'nada'}")
    checks = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    errors = sum(1 for check in checks if check.status == "bad")
    warns = sum(1 for check in checks if check.status == "warn")
    print(f"  después: {errors} error(es) · {warns} aviso(s)")
    try:
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
    except OSError:
        print("  ⚠ No se pudo escribir el informe de setup")
    else:
        print(f"  Informe sin secretos: {path}")
    return 1 if errors else 0


def _run_ai_fix(ctx, *, dry_run: bool, include_tests: bool, assume_yes: bool) -> int:
    """Run the ephemeral agent flow from the command line."""
    from .ai_fix import run_ai_fix

    print("== solventar problemas con IA ==")
    result = run_ai_fix(
        ctx,
        dry_run=dry_run,
        include_tests=include_tests,
        assume_yes=assume_yes,
    )
    if result.applied:
        print(f"  Aplicado: {', '.join(result.applied)}")
    for item in result.manual:
        print(f"  ⚠ pendiente manual: {item}")
    if result.error:
        print(f"  ✗ {result.error}")
    print(f"  Agente desinstalado: {'sí' if result.agent_removed else 'no'}")
    if result.report_path:
        print(f"  Informe sin secretos: {result.report_path}")
    return 1 if result.status == "error" else 0


def _interactive() -> bool:
    """Report whether a curses TUI can be started.

    Returns:
        True when both stdin and stdout are terminals with a usable TERM.
    """
    import os

    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv: list[str] | None = None) -> int:
    """Run the installer.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit status.
    """
    args = _parser().parse_args(argv)

    if args.check:
        return _check(profile=args.profile)

    ctx = load_context()
    if args.profile:
        ctx.profile = next((p for p in PROFILES if p.key == args.profile), ctx.profile)
    if args.install:
        ctx.method = args.install
    if args.fix_conflicts or args.fix_all:
        return _run_fix(ctx, conflicts_only=args.fix_conflicts, assume_yes=args.yes)
    if args.ai_fix:
        return _run_ai_fix(
            ctx,
            dry_run=args.ai_fix_dry_run,
            include_tests=args.ai_fix_with_tests,
            assume_yes=args.yes,
        )

    if args.engines and (args.plain or not _interactive()):
        from .plain import run_engines

        if not sys.stdin.isatty():
            print("(modo línea: no hay terminal interactiva)", file=sys.stderr)
        return run_engines(ctx, assume_yes=args.yes)

    if args.engines:
        from .app import build_engines_app
        from .curses_ui import run_tui

        app = build_engines_app(ctx)
        try:
            return run_tui(app)
        except Exception as exc:  # noqa: BLE001 - curses leaves the screen broken otherwise
            if app.started:
                print(
                    f"\nEl TUI se cerró por un error ({exc}). No se cambia al modo línea "
                    "para no volver a preguntar ni perder los cambios.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"\nNo se pudo iniciar el TUI ({exc}); usando el modo línea.",
                file=sys.stderr,
            )
            from .plain import run_engines

            return run_engines(ctx, assume_yes=args.yes)

    if args.plain or not _interactive():
        from .plain import run_plain

        if not sys.stdin.isatty():
            print("(modo línea: no hay terminal interactiva)", file=sys.stderr)
        return run_plain(
            ctx,
            method_hint=args.install,
            profile_hint=args.profile,
            assume_yes=args.yes,
        )

    from .curses_ui import run_tui

    app = build_app(ctx)
    try:
        return run_tui(app)
    except Exception as exc:  # noqa: BLE001 - curses leaves the screen broken otherwise
        if app.started:
            print(
                f"\nEl TUI se cerró por un error ({exc}). No se cambia al modo línea "
                "para no volver a preguntar ni perder los cambios.",
                file=sys.stderr,
            )
            return 1
        print(f"\nNo se pudo iniciar el TUI ({exc}); usando el modo línea.", file=sys.stderr)
        from .plain import run_plain

        return run_plain(
            ctx,
            method_hint=args.install,
            profile_hint=args.profile,
            assume_yes=args.yes,
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
