"""Environment detection: what the machine already provides.

Everything here is read only — the wizard never installs or changes anything
while it inspects the host.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass, field

from .util import REPO_ROOT, EnvPaths, env_paths, tool_version, which

__all__ = ["Finding", "Report", "detect"]


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


def _docker_status() -> tuple[str, str]:
    """Check the docker binary and its daemon.

    Returns:
        Tuple of ``(status, detail)``.
    """
    if not which("docker"):
        return "bad", "docker no está instalado (hace falta solo para el stack)"
    version = tool_version(["docker", "version", "--format", "{{.Server.Version}}"], timeout=12.0)
    if version:
        return "ok", f"docker {version} con el demonio en marcha"
    if tool_version(["docker", "version"], timeout=12.0):
        return "warn", "docker instalado pero el demonio no responde (¿sudo usermod?)"
    return "warn", "docker instalado; no se pudo leer la versión del servidor"


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

    compose = REPO_ROOT / "deploy" / "docker-compose.yml"
    report.findings.append(
        Finding(
            "compose",
            "ok" if compose.exists() else "bad",
            "deploy/docker-compose.yml encontrado"
            if compose.exists()
            else "falta deploy/docker-compose.yml",
        )
    )

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
