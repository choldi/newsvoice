"""Dependency installation plan and execution.

The plan is data, not actions: the TUI shows the commands it is about to run
and only executes them once the user confirms.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .util import REPO_ROOT, CommandResult, which

__all__ = ["METHODS", "Step", "execute", "plan"]

#: (id, label, description) for each supported installation method.
METHODS: tuple[tuple[str, str, str], ...] = (
    (
        "uv",
        "uv · workspace (recomendado)",
        "`uv sync` instala news, gateway y tts_contract desde uv.lock en un solo .venv raíz.",
    ),
    (
        "venv",
        "pip · un .venv por app",
        "Crea apps/news/.venv y apps/gateway/.venv con `pip install -e .[dev]` "
        "(lo que hace `make venv`).",
    ),
    (
        "skip",
        "Instalar dependencias más tarde",
        "Escribe los .env y comprueba el entorno sin tocar ningún paquete.",
    ),
)


@dataclass
class Step:
    """One command the wizard can run for the user.

    Attributes:
        title: Short heading shown while the step runs.
        command: argv to execute.
        cwd: Working directory (repository root unless stated).
        required: Failure of a required step aborts the plan.
        detail: One line explanation rendered next to the title.
    """

    title: str
    command: list[str]
    cwd: Path = REPO_ROOT
    required: bool = True
    detail: str = ""
    env: dict[str, str] = field(default_factory=dict)


def plan(method: str) -> list[Step]:
    """Build the ordered command list for one installation method.

    Args:
        method: One of the ids in :data:`METHODS`.

    Returns:
        Steps to run in order; empty when the method installs nothing.
    """
    if method == "skip":
        return []
    if method == "venv":
        return [
            Step(
                title="virtualenv news-engine",
                command=["python3", "-m", "venv", ".venv"],
                cwd=REPO_ROOT / "apps" / "news",
                detail="Interpretador aislado para apps/news",
            ),
            Step(
                title="instalar news-engine",
                command=[".venv/bin/python", "-m", "pip", "install", "-e", ".[dev]"],
                cwd=REPO_ROOT / "apps" / "news",
                detail="Dependencias + pytest/ruff",
            ),
            Step(
                title="virtualenv tts-api",
                command=["python3", "-m", "venv", ".venv"],
                cwd=REPO_ROOT / "apps" / "gateway",
                detail="Interpretador aislado para apps/gateway",
            ),
            Step(
                title="instalar tts-api",
                command=[".venv/bin/python", "-m", "pip", "install", "-e", ".[dev]"],
                cwd=REPO_ROOT / "apps" / "gateway",
                detail="Dependencias + pytest/ruff/mypy",
            ),
        ]
    if method == "uv":
        return [
            Step(
                title="uv sync (workspace)",
                command=["uv", "sync", "--all-packages", "--all-extras"],
                cwd=REPO_ROOT,
                detail="Instala todos los paquetes del workspace desde uv.lock",
            ),
        ]
    return []


def verify(method: str) -> list[Step]:
    """Return the smoke-test commands used to confirm an installation.

    Args:
        method: Installation method id.

    Returns:
        Read-only commands run after the plan succeeds.
    """
    if method == "skip":
        return []
    if method == "venv":
        return [
            Step(
                title="verificar news-engine",
                command=[".venv/bin/python", "-c", "import src.api.services.audiobook"],
                cwd=REPO_ROOT / "apps" / "news",
                detail="El paquete importa",
            ),
            Step(
                title="verificar tts-api",
                command=[".venv/bin/python", "-c", "import tts_api"],
                cwd=REPO_ROOT / "apps" / "gateway",
                detail="El paquete importa",
            ),
        ]
    return [
        Step(
            title="verificar workspace",
            command=["uv", "run", "python", "-c", "import src.api.services.audiobook"],
            cwd=REPO_ROOT / "apps" / "news",
            detail="El paquete de news importa desde el venv raíz",
        )
    ]


def method_available(method: str) -> tuple[bool, str]:
    """Tell whether an installation method can run on this host.

    Args:
        method: Installation method id.

    Returns:
        ``(available, reason)`` where the reason explains a refusal.
    """
    if method == "skip":
        return True, ""
    if method == "uv" and not which("uv"):
        return False, "uv no está instalado (curl -LsSf https://astral.sh/uv/install.sh | sh)"
    if method == "venv" and not which("python3"):
        return False, "python3 no está en PATH"
    return True, ""


def execute(
    steps: list[Step],
    emit: Callable[[str, str, str], None],
) -> bool:
    """Run every step, reporting progress through ``emit``.

    Args:
        steps: Ordered commands from :func:`plan` or :func:`verify`.
        emit: Callback receiving ``(status, title, detail)`` where status is
            ``run``, ``ok`` or ``bad``.

    Returns:
        True when every required step succeeded.
    """
    from .util import run

    ok = True
    for step in steps:
        emit("run", step.title, " ".join(str(part) for part in step.command))
        result: CommandResult = run(
            [str(part) for part in step.command],
            cwd=step.cwd,
            timeout=1800.0,
            env=step.env or None,
        )
        if result.ok:
            emit("ok", step.title, step.detail or "correcto")
            continue
        tail = _tail(result.text)
        emit("bad", step.title, tail or "fallo sin salida")
        if step.required:
            ok = False
    return ok


def _tail(text: str, lines: int = 6) -> str:
    """Return the last non empty lines of a command output.

    Args:
        text: Combined stdout/stderr.
        lines: How many lines to keep.

    Returns:
        Shortened output safe to show in a one line status.
    """
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " · ".join(kept[-lines:])[:300]
