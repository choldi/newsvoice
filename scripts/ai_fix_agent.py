"""Ephemeral, standard-library-only repair agent driven by the setup wizard.

The wizard downloads a pinned copy of this file, runs it in a temporary
directory with a minimal environment and deletes it afterwards. It reads a
JSON request, sends the wizard-generated prompt to an OpenAI-compatible
endpoint and writes a JSON response with proposed unified diffs. It never
writes to the repository: applying changes stays under the setup's control.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "AgentRequest",
    "AgentResponse",
    "call_llm",
    "load_request",
    "run",
    "safe_relative_path",
]

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 300.0
MAX_PROMPT_CHARS = 90_000
MAX_FILES = 12
MAX_FILE_CHARS = 12_000

_ALLOWED_SUFFIXES = frozenset(
    {".py", ".toml", ".json", ".md", ".sh", ".yml", ".yaml", ".html", ".js", ".css", ".sql"}
)
_FORBIDDEN_SUFFIXES = (".key", ".pem", ".db", ".gguf", ".bin", ".wav", ".mp3", ".lock")
_FORBIDDEN_DIRS = frozenset({".git", "data", "models", "storage", "__pycache__", "node_modules"})
_SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|_KEY$|API_KEY|PRIVATE|CREDENTIAL)", re.I)
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_FILES = frozenset({"deploy", "news", "gateway"})
_SYSTEM_PROMPT = (
    "Eres un agente de reparación de newsvoice. Propon el mínimo cambio que resuelva "
    "los problemas descritos. Respeta LiteLLM para LLM, sin secretos en código, diffs "
    "pequeños y estilo ruff. Devuelve solo JSON con: status, diagnosis, root_causes, "
    "fixes (path, content, reason, confidence), env_fixes (file, key, value, reason) "
    "y manual. 'content' es el contenido completo y final del archivo; env_fixes solo "
    "para claves no secretas. No incluyas explicaciones fuera del JSON."
)


@dataclass(frozen=True)
class AgentRequest:
    """Inputs supplied by the setup wizard.

    Attributes:
        schema_version: Contract version; mismatches are rejected.
        repo_root: Repository root used to resolve context files.
        prompt: Wizard-generated prompt with the redacted evidence.
        context_files: Additional repository files to append to the prompt.
        allow_paths: Relative prefixes the model may propose to change.
        max_files: Upper bound on context files.
        max_file_chars: Upper bound per context file.
    """

    schema_version: int
    repo_root: Path
    prompt: str
    context_files: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    max_files: int = MAX_FILES
    max_file_chars: int = MAX_FILE_CHARS

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentRequest:
        """Build a request from its JSON representation."""
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"schema_version {raw.get('schema_version')!r} no soportado")
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt vacío")
        files = tuple(str(item) for item in raw.get("context_files", []))
        allow = tuple(str(item) for item in raw.get("allow_paths", []))
        return cls(
            schema_version=SCHEMA_VERSION,
            repo_root=Path(str(raw.get("repo_root", "."))).resolve(),
            prompt=prompt,
            context_files=files,
            allow_paths=allow,
            max_files=max(1, int(raw.get("max_files", MAX_FILES))),
            max_file_chars=max(256, int(raw.get("max_file_chars", MAX_FILE_CHARS))),
        )


@dataclass
class AgentResponse:
    """Result returned to the setup wizard.

    Attributes:
        status: ``ok`` or ``error``.
        diagnosis: Short human-readable summary.
        root_causes: Causes identified from the evidence.
        fixes: Proposed changes with path, content, reason and unified_diff.
        env_fixes: Non-secret configuration keys staged by the wizard.
        manual: Issues that need an operator.
        error: Error message when ``status`` is ``error``.
    """

    status: str
    diagnosis: str = ""
    root_causes: list[str] = field(default_factory=list)
    fixes: list[dict[str, str]] = field(default_factory=list)
    env_fixes: list[dict[str, str]] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable representation."""
        return {
            "status": self.status,
            "diagnosis": self.diagnosis,
            "root_causes": self.root_causes,
            "fixes": self.fixes,
            "env_fixes": self.env_fixes,
            "manual": self.manual,
            "error": self.error,
            "schema_version": SCHEMA_VERSION,
        }


def safe_relative_path(value: object, allow_paths: tuple[str, ...] = ()) -> bool:
    """Report whether a proposed path is allowed inside the repository."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or value.startswith("\\"):
        return False
    if candidate.name.startswith(".env") or candidate.suffix.lower() in _FORBIDDEN_SUFFIXES:
        return False
    if any(part in _FORBIDDEN_DIRS for part in candidate.parts):
        return False
    if candidate.suffix and candidate.suffix.lower() not in _ALLOWED_SUFFIXES:
        return False
    if not allow_paths:
        return True
    return any(
        value == prefix.rstrip("/") or value.startswith(prefix.rstrip("/") + "/")
        for prefix in allow_paths
    )


def load_request(path: Path) -> AgentRequest:
    """Read and validate a request file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("la petición debe ser un objeto JSON")
    return AgentRequest.from_dict(raw)


def _read_context(request: AgentRequest) -> str:
    """Append a bounded snapshot of the listed repository files."""
    chunks: list[str] = []
    for relative in request.context_files[: request.max_files]:
        if not safe_relative_path(relative):
            continue
        path = request.repo_root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > request.max_file_chars:
            text = text[: request.max_file_chars] + "\n… <truncado>"
        chunks.append(f"--- {relative} ---\n{text}")
    return "\n\n".join(chunks)


def _strip_fences(text: str) -> str:
    """Remove Markdown code fences around a JSON answer."""
    body = text.strip()
    if body.startswith("```") and body.endswith("```"):
        body = body[3:-3].strip()
        if body.lower().startswith("json"):
            body = body[4:].strip()
    return body


def call_llm(prompt: str, base_url: str, api_key: str, model: str, timeout: float) -> str:
    """Send one chat completion request and return the message content."""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt[:MAX_PROMPT_CHARS]},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"llamada LLM fallida: {exc}") from exc
    try:
        return str(body["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("respuesta LLM sin contenido") from exc


def _unified_diff(path: str, original: str, updated: str) -> str:
    """Build a git-compatible unified diff from two versions of a file."""
    old_lines = original.splitlines(keepends=True)
    new_lines = updated.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}" if original else "/dev/null",
            tofile=f"b/{path}",
            n=3,
        )
    )


def _valid_fixes(request: AgentRequest, raw_fixes: object) -> list[dict[str, str]]:
    """Validate model-proposed fixes and attach their unified diffs."""
    if not isinstance(raw_fixes, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw_fixes:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not safe_relative_path(path, request.allow_paths):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        target = request.repo_root / str(path)
        try:
            original = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (OSError, UnicodeDecodeError):
            continue
        if content == original:
            continue
        if str(path).endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError:
                continue
        reason = str(item.get("reason", ""))[:1000]
        confidence = str(item.get("confidence", "medium"))[:20]
        result.append(
            {
                "path": str(path),
                "content": content,
                "reason": reason,
                "confidence": confidence,
                "unified_diff": _unified_diff(str(path), original, content),
            }
        )
    return result


def _valid_env_fixes(raw: object) -> list[dict[str, str]]:
    """Validate non-secret configuration changes proposed by the model."""
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_key = str(item.get("file", ""))
        key = str(item.get("key", ""))
        value = item.get("value")
        if file_key not in _ENV_FILES or not _ENV_KEY.match(key) or _SECRET_KEY.search(key):
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        result.append(
            {
                "file": file_key,
                "key": key,
                "value": str(value)[:2000],
                "reason": str(item.get("reason", ""))[:1000],
            }
        )
    return result[:20]


def _strings(raw: object, limit: int = 40) -> list[str]:
    """Return a bounded list of clean strings."""
    if not isinstance(raw, list):
        return []
    return [str(item).strip()[:1000] for item in raw if str(item).strip()][:limit]


def run(
    request: AgentRequest,
    *,
    completer: Callable[[str, str, str, str, float], str] | None = None,
) -> AgentResponse:
    """Analyze the evidence and produce validated proposed changes."""
    base = os.environ.get("AIFIX_API_BASE", "").strip()
    key = os.environ.get("AIFIX_API_KEY", "").strip()
    model = os.environ.get("AIFIX_MODEL", "").strip()
    if not base or not model:
        return AgentResponse(status="error", error="AIFIX_API_BASE y AIFIX_MODEL son obligatorios")
    prompt = request.prompt
    context = _read_context(request)
    if context:
        prompt = f"{prompt}\n\n## Código del repositorio\n\n{context}"
    try:
        answer = (completer or call_llm)(prompt, base, key, model, DEFAULT_TIMEOUT)
        raw = json.loads(_strip_fences(answer))
    except (RuntimeError, ValueError) as exc:
        return AgentResponse(status="error", error=str(exc))
    if not isinstance(raw, dict):
        return AgentResponse(status="error", error="la respuesta LLM no es un objeto JSON")
    return AgentResponse(
        status="ok",
        diagnosis=str(raw.get("diagnosis", ""))[:4000],
        root_causes=_strings(raw.get("root_causes")),
        fixes=_valid_fixes(request, raw.get("fixes")),
        env_fixes=_valid_env_fixes(raw.get("env_fixes")),
        manual=_strings(raw.get("manual")),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the setup wizard."""
    parser = argparse.ArgumentParser(description="Agente efímero de reparación de newsvoice")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        response = run(load_request(args.request))
    except (OSError, ValueError) as exc:
        response = AgentResponse(status="error", error=str(exc))
    payload = json.dumps(response.to_dict(), ensure_ascii=False, indent=2)
    args.out.write_text(payload, encoding="utf-8")
    print(f"estado: {response.status} · {len(response.fixes)} propuesta(s)")
    return 0 if response.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
