"""Orchestrate the ephemeral AI repair flow from the setup wizard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..app import _record_repair
from ..config_report import write_config_report
from ..util import REPO_ROOT
from ..validate import validate
from . import agent, apply, credentials, evidence, prompt

__all__ = ["AiFixResult", "run_ai_fix"]

ALLOW_PATHS = (
    "apps/news/src",
    "apps/news/tests",
    "apps/news/scripts",
    "apps/gateway/tts_api",
    "apps/gateway/tests",
    "libs/tts_contract",
    "scripts/setup_wizard",
)


@dataclass
class AiFixResult:
    """Outcome of one AI repair run.

    Attributes:
        status: ``ok`` or ``error``.
        diagnosis: Agent summary of the problem.
        applied: Paths and env keys changed successfully.
        manual: Problems left for the operator.
        error: Failure message.
        agent_removed: Whether the downloaded agent was deleted.
        report_path: Secret-free report written to disk.
    """

    status: str = "ok"
    diagnosis: str = ""
    applied: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    error: str = ""
    agent_removed: bool = False
    report_path: str = ""


def _confirm_default(question: str) -> bool:
    """Ask for confirmation on stdin, defaulting to no."""
    try:
        answer = input(f"  {question} [s/N]: ").strip().lower()
    except (EOFError, OSError, KeyboardInterrupt):
        print()
        return False
    return answer in ("s", "sí", "si", "y", "yes")


def _error_counts(ctx) -> tuple[int, int]:
    checks = validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)
    return (
        sum(1 for check in checks if check.status == "bad"),
        sum(1 for check in checks if check.status == "warn"),
    )


def _write_report(ctx, result: AiFixResult) -> None:
    """Write a secret-free report of this run."""
    try:
        path = write_config_report(
            ctx.envs,
            ctx.report,
            profile=ctx.profile,
            method=ctx.method,
            checks=validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key),
            repairs=result.applied,
            manual=result.manual,
            path=REPO_ROOT / "reports" / "ai-fix-report.md",
        )
    except OSError:
        return
    result.report_path = str(path)


def run_ai_fix(
    ctx,
    *,
    user_text: str = "",
    include_tests: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
    emit: Callable[[str], None] = print,
    confirm: Callable[[str], bool] | None = None,
    source_path: Path | None = None,
) -> AiFixResult:
    """Run diagnosis, propose corrections and apply them on confirmation."""
    result = AiFixResult()
    try:
        credentials.ensure(ctx, assume_yes=assume_yes, emit=emit)
    except RuntimeError as exc:
        result.status = "error"
        result.error = str(exc)
        return result

    snapshot = apply.capture_envs(ctx)
    before = _error_counts(ctx)
    collected = evidence.collect(ctx, user_text=user_text, include_tests=include_tests, emit=emit)
    if collected.is_empty():
        result.status = "error"
        result.error = "No hay errores, logs ni descripción que analizar"
        return result
    built_prompt = prompt.build_prompt(ctx, collected, allow_paths=ALLOW_PATHS)

    handle: agent.AgentHandle | None = None
    try:
        with agent.ephemeral_agent(emit=emit, source_path=source_path) as handle:
            response = agent.run_agent(
                handle,
                ctx,
                built_prompt,
                context_files=tuple(collected.files),
                allow_paths=ALLOW_PATHS,
                emit=emit,
            )
            if response.get("status") != "ok":
                result.status = "error"
                result.error = str(response.get("error") or "el agente no devolvió correcciones")
            else:
                result.diagnosis = str(response.get("diagnosis", ""))[:4000]
                result.manual = [
                    str(item) for item in response.get("manual", []) if isinstance(item, str)
                ]
                emit(f"  Diagnóstico: {result.diagnosis or 'sin detalle'}")
                for line in apply.preview(response):
                    emit(line)
                has_changes = bool(response.get("fixes") or response.get("env_fixes"))
                if dry_run:
                    emit("  · simulación: no se aplicó ningún cambio")
                elif not has_changes:
                    emit("  · el agente no propuso cambios automáticos")
                else:
                    accepted = (
                        True
                        if assume_yes
                        else (confirm or _confirm_default)("¿Aplicar las correcciones?")
                    )
                    if not accepted:
                        emit("  Correcciones canceladas; no se escribió ningún archivo.")
                    else:
                        staged = apply.stage_env_fixes(ctx, response)
                        if staged is not None:
                            _record_repair(ctx, staged)
                            if staged.error:
                                result.status = "error"
                                result.error = str(staged.error)
                        applied, backups = apply.apply_code_fixes(response, emit=emit)
                        result.applied = list(applied)
                        if staged is not None:
                            result.applied.extend(staged.applied)
                        after = _error_counts(ctx)
                        if result.status == "ok" and after > before:
                            apply.rollback(backups)
                            apply.restore_envs(ctx, snapshot)
                            result.applied = []
                            result.status = "error"
                            result.error = (
                                "Las correcciones empeoraron la validación; se revirtieron"
                            )
                            emit("  ✗ validación empeoró; cambios revertidos")
                        elif result.status == "ok":
                            for key in ("deploy", "news", "gateway"):
                                env = ctx.envs[key]
                                if env.changed_keys():
                                    env.write()
        if handle is not None:
            result.agent_removed = handle.removed
    except (agent.AgentError, OSError, ValueError) as exc:
        result.status = "error"
        result.error = str(exc)
        apply.restore_envs(ctx, snapshot)
    _write_report(ctx, result)
    return result
