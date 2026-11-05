"""Import and export of ``.env`` values for the setup wizard.

Import reads any ``KEY=VALUE`` file and stages the values into the three
wizard env files when the key already exists there. Export dumps every active
value of the three files into one text archive, grouped per file.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["export_text", "export_write", "import_values", "parse_env_text"]

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_text(text: str) -> dict[str, str]:
    """Parse a ``.env`` style file into ``{KEY: VALUE}``.

    Comment lines, blanks and single/double quoted values are handled.

    Args:
        text: Raw file content.

    Returns:
        Mapping of every uncommented key to its unquoted value.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _KEY_RE.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        values[match.group(1)] = _unquote(match.group(2).strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def import_values(ctx, values: dict[str, str]) -> tuple[list[str], list[str]]:
    """Stage ``values`` into every env file that knows the key.

    Keys unknown to the three templates are reported, never appended.

    Args:
        ctx: Wizard context with the loaded env files.
        values: Mapping produced by :func:`parse_env_text`.

    Returns:
        ``(applied, unknown)``: keys changed and keys with no destination.
    """
    applied: list[str] = []
    unknown: list[str] = []
    for key, value in sorted(values.items()):
        targets = [target.key for target in env_targets(ctx) if ctx.envs[target.key].has(key)]
        if not targets:
            unknown.append(key)
            continue
        changed = False
        for file_key in targets:
            env = ctx.envs[file_key]
            if env.get(key).strip() == value and env.staged(key) is None:
                continue
            env.set(key, value)
            changed = True
        if changed:
            applied.append(key)
    return applied, unknown


def env_targets(ctx) -> list:
    """Return the wizard's env targets in their stable order."""
    from .util import env_paths

    return env_paths()


def export_text(ctx) -> str:
    """Render the active values of the three env files as one text block.

    Args:
        ctx: Wizard context with the loaded env files.

    Returns:
        Content ready to write to an export file, grouped per env file.
    """
    lines = ["# newsvoice · configuración de los tres .env"]
    for target in env_targets(ctx):
        env = ctx.envs[target.key]
        lines.append("")
        lines.append(f"# {target.key_path} — {target.label}")
        seen: set[str] = set()
        for entry in env.entries():
            if not entry.key or entry.commented or entry.key in seen:
                continue
            seen.add(entry.key)
            value = env.get(entry.key)
            if value.strip():
                lines.append(f"{entry.key}={value}")
    return "\n".join(lines) + "\n"


def export_write(ctx, path: Path) -> Path:
    """Write the export text to ``path``.

    Args:
        ctx: Wizard context with the loaded env files.
        path: Destination file; parent directories are created.

    Returns:
        The path written.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_text(ctx), encoding="utf-8")
    return path
