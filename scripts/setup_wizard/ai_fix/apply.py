"""Apply agent-proposed corrections under the wizard's control."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from ..util import REPO_ROOT

__all__ = [
    "apply_code_fixes",
    "capture_envs",
    "preview",
    "restore_envs",
    "stage_env_fixes",
]

_Backups = dict[str, bytes | None]


def preview(response: dict[str, object]) -> list[str]:
    """Render a secret-free summary of the proposed changes."""
    lines: list[str] = []
    for item in response.get("fixes", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "?"))
        diff = str(item.get("unified_diff", ""))
        reason = str(item.get("reason", ""))
        lines.append(f"  · {path}: {reason or 'sin motivo declarado'}")
        lines.extend("    " + line for line in diff.splitlines()[:40])
    for item in response.get("env_fixes", []):
        if isinstance(item, dict):
            lines.append(
                f"  · {item.get('file')}.env:{item.get('key')} = {str(item.get('value'))[:60]}"
            )
    return lines


def _git_apply(args: list[str], diff: str) -> tuple[bool, str]:
    """Run git apply with a patch read from stdin."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            input=diff,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    return proc.returncode == 0, proc.stderr.strip()


def apply_code_fixes(
    response: dict[str, object],
    *,
    emit: Callable[[str], None] = print,
) -> tuple[list[str], _Backups]:
    """Apply each proposed diff, rolling back the file when git rejects it."""
    applied: list[str] = []
    backups: _Backups = {}
    for item in response.get("fixes", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", ""))
        diff = str(item.get("unified_diff", ""))
        if not path or not diff:
            continue
        target = REPO_ROOT / path
        backups[path] = target.read_bytes() if target.is_file() else None
        ok, detail = _git_apply(["apply", "--check", "-"], diff)
        if ok:
            ok, detail = _git_apply(["apply", "-"], diff)
        if not ok:
            rollback(backups)
            emit(f"  ✗ {path}: {detail[-300:]}")
            break
        applied.append(path)
        emit(f"  ✓ {path}")
    return applied, backups


def rollback(backups: _Backups) -> None:
    """Restore files to the state captured before applying code fixes."""
    for path, data in backups.items():
        target = REPO_ROOT / path
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(data)


def capture_envs(ctx) -> dict[str, object]:
    """Snapshot the in-memory env files before staging changes."""
    from ..repair import _capture

    return _capture(ctx)


def restore_envs(ctx, snapshot: dict[str, object]) -> None:
    """Restore a snapshot taken by :func:`capture_envs`."""
    from ..repair import _restore

    _restore(ctx, snapshot)


def stage_env_fixes(ctx, response: dict[str, object]) -> object:
    """Stage non-secret env changes using the existing repair mechanism."""
    from ..repair import Fix, apply_fixes

    fixes: list[Fix] = []
    for item in response.get("env_fixes", []):
        if not isinstance(item, dict):
            continue
        file_key = str(item.get("file", ""))
        key = str(item.get("key", ""))
        if file_key not in ctx.envs or not key:
            continue

        def apply_change(context, *, _file=file_key, _key=key, _value=str(item.get("value", ""))):
            context.envs[_file].set(_key, _value)
            return f"{_file}/.env:{_key}"

        fixes.append(
            Fix(
                id=f"aifix:{file_key}:{key}",
                label=f"Ajustar {file_key}/.env:{key}",
                detail=str(item.get("reason", ""))[:200] or "sugerido por el agente",
                apply=apply_change,
            )
        )
    return apply_fixes(ctx, fixes) if fixes else None
