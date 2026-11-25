"""Ask for and persist the OpenAI-compatible endpoint used by the AI repair."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import Context

__all__ = ["KEYS", "ensure", "read"]

KEYS = ("AI_FIX_API_BASE", "AI_FIX_MODEL", "AI_FIX_API_KEY")


def read(ctx: Context) -> dict[str, str]:
    """Return the three current values from the news env file."""
    env = ctx.envs.get("news")
    return {key: (env.get(key).strip() if env else "") for key in KEYS}


def _ask(label: str, current: str, *, secret: bool = False) -> str:
    """Prompt for one value, keeping an existing secret hidden."""
    shown = "••••" if secret and current else current or "vacío"
    try:
        answer = input(f"  {label} [{shown}]: ").strip()
    except (EOFError, OSError, KeyboardInterrupt):
        print()
        return current
    return answer or current


def ensure(ctx: Context, *, assume_yes: bool = False, emit=print) -> dict[str, str]:
    """Collect missing values and stage them in ``apps/news/.env``.

    Args:
        ctx: Current wizard state.
        assume_yes: Skip prompts when the values are already present.
        emit: Progress callback.

    Returns:
        The effective values.

    Raises:
        RuntimeError: When a value is missing in a non-interactive run.
    """
    from ..app import _mask

    values = read(ctx)
    for name, env_name in (
        ("AI_FIX_API_BASE", "AIFIX_API_BASE"),
        ("AI_FIX_MODEL", "AIFIX_MODEL"),
        ("AI_FIX_API_KEY", "AIFIX_API_KEY"),
    ):
        values[name] = os.environ.get(env_name, "").strip() or values[name]
    if not assume_yes and sys.stdin.isatty():
        emit("  Datos del modelo para «solventar problemas con IA»:")
        values["AI_FIX_API_BASE"] = _ask("URL base (incluye /v1)", values["AI_FIX_API_BASE"])
        values["AI_FIX_MODEL"] = _ask("Modelo exacto", values["AI_FIX_MODEL"])
        key = _ask("Clave API (vacía = sin autenticación)", "", secret=True)
        if not key and values["AI_FIX_API_KEY"]:
            emit(f"  Clave existente conservada: {_mask(values['AI_FIX_API_KEY'])}")
        elif key:
            values["AI_FIX_API_KEY"] = key
    if not values["AI_FIX_API_BASE"] or not values["AI_FIX_MODEL"]:
        raise RuntimeError("Faltan AI_FIX_API_BASE o AI_FIX_MODEL")
    env = ctx.envs.get("news")
    if env is not None:
        for name in KEYS:
            env.set(name, values[name])
    return values
