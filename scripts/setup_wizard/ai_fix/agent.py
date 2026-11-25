"""Download, run and remove the pinned ephemeral repair agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..util import REPO_ROOT

__all__ = ["AgentError", "AgentHandle", "ephemeral_agent", "load_manifest", "run_agent"]

_MAX_BYTES = 2 * 1024 * 1024
_TIMEOUT = 330.0
_MANIFEST = Path(__file__).with_name("manifest.json")


class AgentError(RuntimeError):
    """Raised when the agent cannot be downloaded, run or parsed."""


@dataclass
class AgentHandle:
    """A downloaded agent living in a temporary directory.

    Attributes:
        path: Executable script path.
        root: Temporary directory that owns every downloaded artifact.
        removed: True once the cleanup ran.
    """

    path: Path
    root: Path
    removed: bool = False

    def cleanup(self) -> None:
        """Delete the temporary directory; safe to call more than once."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.removed = True


def load_manifest() -> dict[str, str]:
    """Read the pinned URL and checksum of the agent."""
    raw = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {
        "version": str(raw["version"]),
        "filename": str(raw["filename"]),
        "url": os.environ.get("AIFIX_AGENT_URL", "").strip() or str(raw["url"]),
        "sha256": os.environ.get("AIFIX_AGENT_SHA256", "").strip() or str(raw["sha256"]),
    }


def _download(url: str, sha256: str, destination: Path) -> None:
    """Fetch the agent over HTTPS and verify its pinned SHA256."""
    if not url.startswith("https://"):
        raise AgentError("la URL del agente debe usar HTTPS")
    try:
        with urllib.request.urlopen(url, timeout=60.0) as response:
            if not response.geturl().startswith("https://"):
                raise AgentError("la redirección del agente salió de HTTPS")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise AgentError("el agente descargado excede 2 MB")
                chunks.append(chunk)
    except OSError as exc:
        raise AgentError(f"no se pudo descargar el agente: {exc}") from exc
    body = b"".join(chunks)
    digest = hashlib.sha256(body).hexdigest()
    if digest != sha256.lower():
        raise AgentError(f"SHA256 del agente no coincide: {digest}")
    destination.write_bytes(body)


@contextmanager
def ephemeral_agent(
    *,
    emit: Callable[[str], None] = print,
    source_path: Path | None = None,
) -> Iterator[AgentHandle]:
    """Install the agent in a temporary directory and always remove it."""
    manifest = load_manifest()
    root = Path(tempfile.mkdtemp(prefix="newsvoice-aifix-"))
    handle = AgentHandle(path=root / manifest["filename"], root=root)
    try:
        if source_path is not None:
            handle.path.write_bytes(Path(source_path).read_bytes())
            emit("  · agente local copiado al directorio temporal")
        else:
            emit(f"  · descargando agente efímero v{manifest['version']}…")
            _download(manifest["url"], manifest["sha256"], handle.path)
            emit("  · agente verificado (SHA256)")
        yield handle
    finally:
        handle.cleanup()
        emit(f"  · agente desinstalado: {'sí' if handle.removed else 'no'}")


def _minimal_env(ctx) -> dict[str, str]:
    """Build the only environment the downloaded agent is allowed to see."""
    news = ctx.envs.get("news")
    values = {
        key: (news.get(key).strip() if news else "")
        for key in ("AI_FIX_API_BASE", "AI_FIX_API_KEY", "AI_FIX_MODEL")
    }
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for name, value in values.items():
        env[name] = os.environ.get("AIFIX_" + name.removeprefix("AI_FIX_"), "") or value
    return env


def run_agent(
    handle: AgentHandle,
    ctx,
    prompt: str,
    *,
    context_files: tuple[str, ...],
    allow_paths: tuple[str, ...],
    emit: Callable[[str], None] = print,
) -> dict[str, object]:
    """Run the agent once and return its parsed response."""
    request = {
        "schema_version": 1,
        "repo_root": str(REPO_ROOT),
        "prompt": prompt,
        "context_files": list(context_files),
        "allow_paths": list(allow_paths),
        "max_files": 12,
        "max_file_chars": 12_000,
    }
    request_path = handle.root / "request.json"
    response_path = handle.root / "response.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    emit("  · analizando con el modelo configurado…")
    try:
        command = [
            sys.executable,
            str(handle.path),
            "--request",
            str(request_path),
            "--out",
            str(response_path),
        ]
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=_minimal_env(ctx),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(f"fallo al ejecutar el agente: {exc}") from exc
    if not response_path.is_file():
        raise AgentError((proc.stderr or proc.stdout or "sin salida del agente").strip()[-1000:])
    raw = json.loads(response_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AgentError("la respuesta del agente no es un objeto JSON")
    if proc.returncode != 0 and not raw.get("error"):
        raw["error"] = (proc.stderr or proc.stdout or "salida no válida").strip()[:1000]
    return raw
