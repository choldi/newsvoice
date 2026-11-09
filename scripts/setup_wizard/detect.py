"""Environment detection: what the machine already provides.

Detection itself is read only. Turning the findings into commands the
operator can run (or let the wizard run for them) lives in
:func:`prerequisites`.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .util import REPO_ROOT, EnvPaths, env_paths, tool_version, which

__all__ = ["Finding", "Prerequisite", "Report", "detect", "prerequisites"]


@dataclass
class Finding:
    """One detected prerequisite.

    Attributes:
        name: Short label shown in the list.
        status: ``ok``, ``warn``, ``bad`` or ``info``.
        detail: Human explanation of what was found.
    """

    name: str
    status: str
    detail: str


@dataclass
class Report:
    """Everything the wizard learned about the host.

    Attributes:
        findings: Prerequisites, in display order.
        python: Interpreter version string in use.
        env_state: One entry per env file: path, exists and non-empty flag.
        venvs: Repo relative paths that currently hold a virtualenv.
        envs: The three env targets.
        ports: Port number to ``occupied`` flag for the published ports.
    """

    findings: list[Finding] = field(default_factory=list)
    python: str = ""
    env_state: dict[str, tuple[str, bool]] = field(default_factory=dict)
    venvs: list[str] = field(default_factory=list)
    envs: list[EnvPaths] = field(default_factory=list)
    ports: dict[int, bool] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        """Count of hard failures."""
        return sum(1 for item in self.findings if item.status == "bad")

    @property
    def warnings(self) -> int:
        """Count of soft warnings."""
        return sum(1 for item in self.findings if item.status == "warn")


def _port_busy(port: int) -> bool:
    """Report whether something already listens on ``127.0.0.1:port``.

    Args:
        port: TCP port to probe.

    Returns:
        True when the port accepts connections.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _is_wsl() -> bool:
    """Report whether this process runs inside the Windows Subsystem for Linux.

    Returns:
        True when the kernel banner mentions Microsoft/WSL.
    """
    try:
        banner = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in banner or "wsl" in banner


def _docker_status() -> tuple[str, str]:
    """Check the docker binary, its client and its daemon.

    ``docker --version`` only exercises the client binary, while every other
    docker subcommand talks to the daemon: on WSL2 those two regularly
    disagree (Docker Desktop installed but its integration with this distro
    switched off), so they are probed separately.

    Returns:
        Tuple of ``(status, detail)``.
    """
    if not which("docker"):
        return "bad", "docker no está instalado (hace falta solo para el stack)"

    # Cliente: no toca el demonio, responde aunque Docker Desktop esté parado.
    client = tool_version(["docker", "--version"])
    # Demonio: `docker version` sale con rc != 0 si el socket no responde.
    server = tool_version(["docker", "version", "--format", "{{.Server.Version}}"], timeout=8.0)

    if server:
        return "ok", f"docker {server} con el demonio en marcha"
    if _is_wsl():
        return (
            "warn",
            f"{client or 'cliente docker'} instalado, pero esta distro WSL no alcanza el "
            "demonio: activa Settings → Resources → WSL Integration de Docker Desktop "
            "(«Use WSL 2 based engine») y aplícalo; si usas docker nativo en WSL, "
            "arranca el servicio (sudo service docker start)",
        )
    if client:
        return (
            "warn",
            f"{client} (cliente) pero el demonio no responde: ¿Docker Desktop "
            "o docker.service arrancado? (sudo systemctl start docker · "
            "sudo usermod -aG docker $USER)",
        )
    return "warn", "docker instalado; no se pudo leer la versión del cliente"


def detect() -> Report:
    """Inspect the host and return a :class:`Report`.

    Returns:
        Detected tools, env files, virtualenvs and busy ports.
    """
    report = Report(python=".".join(str(part) for part in sys.version_info[:3]), envs=env_paths())

    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        report.findings.append(Finding("Python", "ok", f"{report.python} (≥ 3.11 requerido)"))
    else:
        report.findings.append(
            Finding("Python", "bad", f"{report.python} es demasiado viejo: hace falta 3.11+")
        )

    uv = tool_version(["uv", "--version"])
    report.findings.append(
        Finding("uv", "ok", uv) if uv else Finding("uv", "warn", "no está en PATH (opcional)")
    )

    status, detail = _docker_status()
    report.findings.append(Finding("docker", status, detail))

    ffmpeg = which("ffmpeg")
    report.findings.append(
        Finding("ffmpeg", "ok", "ffmpeg disponible")
        if ffmpeg
        else Finding("ffmpeg", "bad", "ffmpeg no está en PATH: hace falta para audio")
    )

    git = tool_version(["git", "--version"])
    report.findings.append(
        Finding("git", "ok", git) if git else Finding("git", "warn", "git no disponible")
    )

    gpu = tool_version(["nvidia-smi", "-L"], timeout=8.0)
    report.findings.append(
        Finding("GPU", "ok", gpu.replace("\n", ", "))
        if gpu
        else Finding("GPU", "warn", "sin nvidia-smi: los engines de GPU no funcionarán")
    )

    compose_file = REPO_ROOT / "deploy" / "docker-compose.yml"
    if not compose_file.exists():
        compose_status, compose_detail = "bad", "falta deploy/docker-compose.yml"
    elif not which("docker"):
        compose_status, compose_detail = "warn", "hace falta Docker (lleva el plugin compose)"
    elif tool_version(["docker", "compose", "version"]):
        compose_status, compose_detail = "ok", "docker compose disponible"
    else:
        compose_status = "bad"
        compose_detail = (
            "docker sin el plugin Compose v2 "
            "(sudo apt-get install -y docker-compose-plugin)"
        )
    report.findings.append(Finding("compose", compose_status, compose_detail))

    for target in report.envs:
        present = target.path.exists() and target.path.stat().st_size > 0
        report.env_state[target.key] = (target.key_path, present)
        report.findings.append(
            Finding(
                f".env {target.key}",
                "ok" if present else "info",
                f"{target.key_path} listo"
                if present
                else f"{target.key_path} se creará desde su plantilla",
            )
        )

    for rel in (".venv", "apps/news/.venv", "apps/gateway/.venv"):
        if (REPO_ROOT / rel / "bin" / "python").exists() or (
            REPO_ROOT / rel / "Scripts" / "python.exe"
        ).exists():
            report.venvs.append(rel)
    report.findings.append(
        Finding(
            "virtualenvs",
            "ok" if report.venvs else "warn",
            ", ".join(report.venvs) if report.venvs else "ninguno: falta instalar dependencias",
        )
    )

    for port in _published_ports(report):
        busy = _port_busy(port)
        report.ports[port] = busy
        if busy:
            report.findings.append(
                Finding(f"puerto {port}", "warn", "ya está en uso (¿stack en marcha?)")
            )

    return report


def _published_ports(report: Report) -> list[int]:
    """Return the ports the wizard should probe.

    Args:
        report: Detection report whose env files may define the ports.

    Returns:
        Sorted list of port numbers.
    """
    ports = {8000, 8001}
    for target in report.envs:
        if target.key != "deploy" or not target.path.exists():
            continue
        for line in target.path.read_text(encoding="utf-8").splitlines():
            for key in ("API_PORT", "GATEWAY_PORT"):
                if line.startswith(f"{key}="):
                    try:
                        ports.add(int(line.split("=", 1)[1].strip()))
                    except ValueError:
                        pass
    return sorted(ports)


# -- prerequisites ----------------------------------------------------------


@dataclass(frozen=True)
class Prerequisite:
    """One prerequisite together with the command that provides it.

    Attributes:
        key: Stable id used by the install step.
        name: Label shown in the list.
        status: ``ok``, ``warn`` or ``bad`` as detected.
        required: True when the Docker stack cannot run without it.
        detail: What the host inspection found.
        why: What breaks when the prerequisite is missing.
        remedy: Commands that install it, in order (empty = manual only).
    """

    key: str
    name: str
    status: str
    required: bool
    detail: str
    why: str
    remedy: tuple[str, ...] = ()

    @property
    def missing(self) -> bool:
        """True when the operator still has to do something about it."""
        return self.status != "ok"

    @property
    def automated(self) -> bool:
        """True when the wizard can run the fix itself."""
        return self.missing and bool(self.remedy)

    @property
    def symbol(self) -> str:
        """Short status symbol for one line listings."""
        return {"ok": "✓", "warn": "⚠", "bad": "✗"}.get(self.status, "·")


def prerequisites(report: Report) -> list[Prerequisite]:
    """Turn detection findings into actionable prerequisites.

    Args:
        report: Host inspection produced by :func:`detect`.

    Returns:
        One entry per prerequisite, in the order they should be presented.
    """
    found = {item.name: item for item in report.findings}

    def state(name: str) -> tuple[str, str]:
        item = found.get(name)
        return ("ok", "") if item is None else (item.status, item.detail)

    items: list[Prerequisite] = []

    status, detail = state("Python")
    items.append(
        Prerequisite(
            key="python",
            name="Python",
            status=status,
            required=True,
            detail=detail,
            why="El asistente y las aplicaciones exigen Python 3.11 o superior.",
            remedy=(),
        )
    )

    status, detail = state("docker")
    if status == "ok":
        remedy: tuple[str, ...] = ()
    elif which("docker") and _is_wsl():
        # Docker Desktop se integra desde su GUI; un `systemctl start` solo
        # sirve si el motor es nativo dentro de WSL.
        remedy = ()
    elif which("docker"):
        remedy = ("sudo systemctl start docker", "sudo usermod -aG docker $USER")
    else:
        remedy = (
            "curl -fsSL https://get.docker.com | sh",
            "sudo usermod -aG docker $USER",
        )
    items.append(
        Prerequisite(
            key="docker",
            name="docker",
            status=status,
            required=True,
            detail=detail,
            why="Las 13 imágenes del stack se levantan con Docker Compose.",
            remedy=remedy,
        )
    )

    status, detail = state("compose")
    items.append(
        Prerequisite(
            key="compose",
            name="Docker Compose v2",
            status=status,
            required=True,
            detail=detail,
            why="deploy/docker-compose.yml usa la sintaxis del plugin v2 (`docker compose`).",
            remedy=("sudo apt-get install -y docker-compose-plugin",)
            if status != "ok"
            else (),
        )
    )

    status, detail = state("uv")
    items.append(
        Prerequisite(
            key="uv",
            name="uv",
            status=status,
            required=False,
            detail=detail,
            why="Instala el workspace y ejecuta tests/lint (recomendado; Docker no lo necesita).",
            remedy=("curl -LsSf https://astral.sh/uv/install.sh | sh",)
            if status != "ok"
            else (),
        )
    )

    status, detail = state("ffmpeg")
    items.append(
        Prerequisite(
            key="ffmpeg",
            name="ffmpeg",
            status=status,
            required=False,
            detail=detail,
            why="Mezcla y transcode de audio en tareas locales (en Docker ya va en la imagen).",
            remedy=("sudo apt-get install -y ffmpeg",) if status != "ok" else (),
        )
    )

    status, detail = state("git")
    items.append(
        Prerequisite(
            key="git",
            name="git",
            status=status,
            required=False,
            detail=detail,
            why="Clonar el repo y leer el historial; el propio asistente no lo necesita.",
            remedy=("sudo apt-get install -y git",) if status != "ok" else (),
        )
    )

    status, detail = state("GPU")
    items.append(
        Prerequisite(
            key="gpu",
            name="GPU NVIDIA",
            status=status,
            required=False,
            detail=detail,
            why="Sin GPU solo funcionan los engines CPU/cloud (edge, pocket_tts, kokoro, "
            "fish_tts, openai_tts); qwen3_tts y omnivoice locales no usan CUDA.",
            remedy=(),
        )
    )

    status, detail = state("virtualenvs")
    if which("uv"):
        remedy = ("uv sync --all-packages --all-extras",)
    elif which("make"):
        remedy = ("make venv",)
    else:
        remedy = ()
    items.append(
        Prerequisite(
            key="virtualenvs",
            name="virtualenvs",
            status=status,
            required=False,
            detail=detail,
            why="Los tests, el lint y las apps en local corren desde el venv; "
            "el stack Docker no lo necesita.",
            remedy=remedy if status != "ok" else (),
        )
    )

    return items
