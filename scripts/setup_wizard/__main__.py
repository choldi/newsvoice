"""Command line entry point: ``python3 scripts/setup.py`` or ``-m setup_wizard``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import build_app, load_context
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
    print(f"Python {report.python}")
    for finding in report.findings:
        print(f"  {_SYMBOLS.get(finding.status, '·')} {finding.name}: {finding.detail}")
    for port, busy in report.ports.items():
        if busy:
            print(f"  ⚠ puerto {port}: ya está en uso")


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

    try:
        return run_tui(build_app(ctx))
    except Exception as exc:  # noqa: BLE001 - curses leaves the screen broken otherwise
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
