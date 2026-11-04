"""Small shared helpers: repository paths and external command inspection."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CommandResult",
    "REPO_ROOT",
    "EnvPaths",
    "generate_secret",
    "repo_root",
    "run",
    "tool_version",
    "which",
]


def repo_root() -> Path:
    """Return the repository root (two levels above this package).

    Returns:
        Absolute path of the directory holding ``apps/``, ``deploy/`` and
        ``scripts/``.
    """
    return Path(__file__).resolve().parents[2]


REPO_ROOT: Path = repo_root()


@dataclass(frozen=True)
class EnvPaths:
    """The three env files the wizard owns, plus their templates.

    Attributes:
        key: Stable id used by the CLI/TUI (``deploy``, ``news``, ``gateway``).
        path: Target file that is written at the end of the wizard.
        example: Template the target starts from when it does not exist yet.
        label: Human readable name shown in the UI.
        key_path: Path shown to the user relative to the repository root.
    """

    key: str
    path: Path
    example: Path
    label: str

    @property
    def key_path(self) -> str:
        """Return the repository-relative path of the target file."""
        return str(self.path.relative_to(REPO_ROOT))


def env_paths() -> list[EnvPaths]:
    """Return the wizard's env targets in reading order.

    Returns:
        One :class:`EnvPaths` per file the assistant walks through.
    """
    return [
        EnvPaths(
            key="deploy",
            path=REPO_ROOT / "deploy" / ".env",
            example=REPO_ROOT / "deploy" / ".env.example",
            label="Compose (puertos, rutas, GPU)",
        ),
        EnvPaths(
            key="news",
            path=REPO_ROOT / "apps" / "news" / ".env",
            example=REPO_ROOT / "apps" / "news" / ".env.example",
            label="news-engine (LLM, admin, entrega, TTS)",
        ),
        EnvPaths(
            key="gateway",
            path=REPO_ROOT / "apps" / "gateway" / ".env",
            example=REPO_ROOT / "apps" / "gateway" / ".env.example",
            label="TTS gateway (engine, GPU, API key)",
        ),
    ]


def which(name: str) -> str | None:
    """Locate an executable on ``PATH``.

    Args:
        name: Program name to resolve.

    Returns:
        Absolute path, or ``None`` when the program is not installed.
    """
    return shutil.which(name)


def generate_secret(length: int = 32) -> str:
    """Return a strong random hex secret.

    Uses ``secrets.token_hex`` so the wizard never depends on ``openssl``
    being installed.

    Args:
        length: Number of random bytes (the hex result is twice as long).

    Returns:
        Lowercase hex string.
    """
    return secrets.token_hex(length)


def tool_version(command: list[str], timeout: float = 10.0) -> str | None:
    """Run a command and return the first line of its output.

    Args:
        command: argv to execute.
        timeout: Seconds before giving up.

    Returns:
        Trimmed first line, or ``None`` when the command failed or timed out.
    """
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output.splitlines()[0] if output else None


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command.

    Attributes:
        ok: True when the process exited with status 0.
        text: Combined stdout/stderr, already trimmed.
    """

    ok: bool
    text: str


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 600.0,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a command capturing its output.

    Args:
        command: argv to execute.
        cwd: Working directory, defaults to the current one.
        timeout: Seconds before the command is killed.
        env: Extra environment variables merged over ``os.environ``.

    Returns:
        A :class:`CommandResult` with the exit status and combined output.
    """
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged,
        )
    except FileNotFoundError as exc:
        return CommandResult(ok=False, text=str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(ok=False, text=f"time out after {timeout:g}s")
    return CommandResult(
        ok=proc.returncode == 0,
        text=((proc.stdout or "") + (proc.stderr or "")).strip(),
    )
