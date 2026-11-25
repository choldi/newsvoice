"""Collect the evidence used to build the wizard-generated repair prompt."""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..util import REPO_ROOT, run

__all__ = ["Evidence", "collect"]

_PATH_RE = re.compile(r"(?:apps|libs|scripts)/[A-Za-z0-9_./-]+\.(?:py|toml|json|sh)")
_MAX_CHARS = 24_000


@dataclass
class Evidence:
    """Redaction-ready snapshot of the current problems.

    Attributes:
        checks: Validation findings.
        deploy_report: Output of ``scripts/deploy_report.sh -``.
        logs: Recent structured log lines.
        tests: Test and lint failures.
        user_text: Problem description pasted by the operator.
        files: Candidate repository files to include in the prompt.
    """

    checks: list[str] = field(default_factory=list)
    deploy_report: str = ""
    logs: str = ""
    tests: str = ""
    user_text: str = ""
    files: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Report whether no evidence at all was collected."""
        return not any((self.checks, self.deploy_report, self.logs, self.tests, self.user_text))


def _clip(text: str, limit: int = _MAX_CHARS) -> str:
    """Keep the tail of a potentially large command output."""
    body = text.strip()
    return body[-limit:] if len(body) > limit else body


def _checks(ctx) -> list[str]:
    from ..validate import validate

    checks = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    return [
        f"{check.status}: {check.name}: {check.detail}"
        for check in checks
        if check.status in ("bad", "warn")
    ]


def _deploy_report() -> str:
    script = REPO_ROOT / "scripts" / "deploy_report.sh"
    if not script.is_file():
        return ""
    result = run(["bash", str(script), "-"], cwd=REPO_ROOT, timeout=180.0)
    return _clip(result.text)


def _logs() -> str:
    log_dir = REPO_ROOT / "apps" / "news" / "data" / "logs"
    if not log_dir.is_dir():
        return ""
    paths = [log_dir / "news_engine.jsonl"]
    paths.extend(sorted(log_dir.glob("monitor_*.jsonl"), reverse=True)[:2])
    chunks: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if lines:
            chunks.append(f"--- {path.name} ---\n" + "\n".join(lines[-40:]))
    return _clip("\n\n".join(chunks))


def _tests(include_tests: bool) -> str:
    if not shutil.which("uv"):
        return "uv no disponible: no se ejecutaron tests ni lint"
    news = REPO_ROOT / "apps" / "news"
    outputs: list[str] = []
    if include_tests:
        result = run(["uv", "run", "pytest", "-q"], cwd=news, timeout=900.0)
        outputs.append("pytest: " + (result.text if not result.ok else "sin fallos"))
    lint = run(["uv", "run", "ruff", "check", "."], cwd=news, timeout=300.0)
    outputs.append("ruff: " + (lint.text if not lint.ok else "sin hallazgos"))
    return _clip("\n\n".join(outputs))


def _files(user_text: str) -> list[str]:
    candidates: list[str] = []
    result = run(["git", "status", "--short"], cwd=REPO_ROOT, timeout=60.0)
    if result.ok:
        for line in result.text.splitlines():
            path = line[3:].strip().split(" -> ")[-1]
            if re.match(r"^(apps|libs|scripts)/", path) and Path(path).suffix == ".py":
                candidates.append(path)
    candidates.extend(_PATH_RE.findall(user_text))
    return list(dict.fromkeys(candidates))[:12]


def collect(
    ctx,
    *,
    user_text: str = "",
    include_tests: bool = False,
    emit: Callable[[str], None] = print,
) -> Evidence:
    """Gather diagnostics from the wizard, logs, reports, tests and operator."""
    emit("  · recogiendo validación, informe y logs…")
    evidence = Evidence(
        checks=_checks(ctx),
        deploy_report=_deploy_report(),
        logs=_logs(),
        tests=_tests(include_tests),
        user_text=user_text.strip()[:8000],
    )
    evidence.files = _files(evidence.user_text)
    if sys.stdin.isatty() and not evidence.user_text:
        emit("  Describe el problema (línea vacía para terminar):")
        lines: list[str] = []
        try:
            while True:
                line = input("  > ")
                if not line.strip():
                    break
                lines.append(line)
        except (EOFError, OSError, KeyboardInterrupt):
            pass
        evidence.user_text = "\n".join(lines)[:8000]
        evidence.files = _files(evidence.user_text)
    return evidence
