"""Tests for the ephemeral AI repair agent and its setup integration."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from ai_fix_agent import AgentRequest, safe_relative_path
from ai_fix_agent import run as run_agent
from setup_wizard import __main__ as cli
from setup_wizard.ai_fix import agent as agent_module
from setup_wizard.ai_fix import flow
from setup_wizard.ai_fix.evidence import Evidence
from setup_wizard.ai_fix.prompt import build_prompt
from setup_wizard.app import Context
from setup_wizard.detect import Report
from setup_wizard.envfile import EnvFile

REPO = Path(__file__).resolve().parents[4]
ENV_FILES = {
    "deploy": "deploy/.env.example",
    "news": "apps/news/.env.example",
    "gateway": "apps/gateway/.env.example",
}


def _context(root: Path) -> Context:
    """Build a context backed by copies of the real env templates."""
    envs: dict[str, EnvFile] = {}
    for key, relative in ENV_FILES.items():
        example = root / relative
        target = example.with_name(".env")
        example.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, example)
        envs[key] = EnvFile.load(target, example)
    return Context(report=Report(python="3.12.0"), envs=envs)


def _request(root: Path, prompt: str = "diagnostica") -> AgentRequest:
    return AgentRequest(schema_version=1, repo_root=root, prompt=prompt)


def _completer(answer: dict[str, object]):
    def complete(_prompt: str, _base: str, _key: str, _model: str, _timeout: float) -> str:
        return json.dumps(answer)

    return complete


def test_cli_exposes_ai_fix_flags() -> None:
    """The repair flow is reachable as an explicit setup flag."""
    args = cli._parser().parse_args(
        ["--ai-fix", "--ai-fix-dry-run", "--ai-fix-with-tests", "--yes"]
    )
    assert args.ai_fix
    assert args.ai_fix_dry_run
    assert args.ai_fix_with_tests
    assert args.yes


def test_manifest_hash_matches_pinned_agent() -> None:
    """The manifest can never drift from the reviewed agent source."""
    manifest = agent_module.load_manifest()
    digest = hashlib.sha256((REPO / "scripts" / "ai_fix_agent.py").read_bytes()).hexdigest()
    assert manifest["sha256"] == digest


def test_safe_path_rejects_secrets_and_escapes() -> None:
    """Proposed paths stay inside the repository allowlist."""
    assert safe_relative_path("apps/news/src/core/llm.py", ("apps/news/src",))
    assert not safe_relative_path("apps/news/.env", ("apps/news/src",))
    assert not safe_relative_path("../deploy/.env", ("apps/news/src",))
    assert not safe_relative_path("apps/news/data/news.db", ("apps/news/src",))
    assert not safe_relative_path("apps/news/src/core/llm.py", ("apps/gateway",))


def test_agent_turns_valid_model_answer_into_diff(tmp_path, monkeypatch) -> None:
    """Valid proposals are converted to a git-compatible unified diff."""
    monkeypatch.setenv("AIFIX_API_BASE", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("AIFIX_MODEL", "openai/local-model")
    target = tmp_path / "module.py"
    target.write_text("valor = 1\n", encoding="utf-8")
    answer = {
        "status": "ok",
        "diagnosis": "constante incorrecta",
        "fixes": [{"path": "module.py", "content": "valor = 2\n", "reason": "corregir"}],
        "env_fixes": [],
        "manual": [],
    }
    result = run_agent(_request(tmp_path), completer=_completer(answer))
    assert result.status == "ok"
    assert len(result.fixes) == 1
    assert result.fixes[0]["unified_diff"].startswith("--- a/module.py")
    assert "+valor = 2" in result.fixes[0]["unified_diff"]


def test_agent_discards_invalid_python(tmp_path, monkeypatch) -> None:
    """Syntax errors proposed by the model never reach the setup."""
    monkeypatch.setenv("AIFIX_API_BASE", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("AIFIX_MODEL", "openai/local-model")
    answer = {
        "status": "ok",
        "fixes": [{"path": "roto.py", "content": "def roto(:\n  pass\n"}],
        "env_fixes": [{"file": "news", "key": "LLM_API_KEY", "value": "secreto"}],
        "manual": [],
    }
    result = run_agent(_request(tmp_path), completer=_completer(answer))
    assert result.fixes == []
    assert result.env_fixes == []


def test_prompt_redacts_secret_values(tmp_path) -> None:
    """Credential values are removed before the prompt leaves the host."""
    ctx = _context(tmp_path)
    ctx.envs["news"].set("AI_FIX_API_KEY", "sk-super-secreta-1234")
    evidence = Evidence(
        checks=["bad: LLM_API_KEY=sk-super-secreta-1234"],
        user_text="el token es sk-super-secreta-1234",
    )
    text = build_prompt(ctx, evidence, allow_paths=("apps/news/src",))
    assert "sk-super-secreta-1234" not in text
    assert "<redactado>" in text


def test_ephemeral_agent_is_removed(tmp_path) -> None:
    """A local source copy is still deleted when the context exits."""
    source = tmp_path / "copia.py"
    source.write_text("print('hola')\n", encoding="utf-8")
    with agent_module.ephemeral_agent(emit=lambda _message: None, source_path=source) as handle:
        assert handle.path.is_file()
        temporary = handle.root
    assert handle.removed
    assert not temporary.exists()


def test_download_rejects_wrong_hash(tmp_path, monkeypatch) -> None:
    """A checksum mismatch aborts the installation."""

    class FakeResponse:
        def __init__(self) -> None:
            self.sent = False

        def geturl(self) -> str:
            return "https://example.test/ai_fix_agent.py"

        def read(self, _size: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b"print(1)\n"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        agent_module.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse()
    )
    with pytest.raises(agent_module.AgentError, match="SHA256"):
        agent_module._download(
            "https://example.test/ai_fix_agent.py", "0" * 64, tmp_path / "agente.py"
        )


def test_dry_run_does_not_apply_changes(tmp_path, monkeypatch) -> None:
    """Simulation reports proposals but leaves the repository untouched."""
    ctx = _context(tmp_path)
    monkeypatch.setenv("AIFIX_API_BASE", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("AIFIX_MODEL", "openai/local-model")
    monkeypatch.setattr(
        flow.evidence,
        "collect",
        lambda *_args, **_kwargs: Evidence(checks=["bad: fallo"], user_text="arregla esto"),
    )
    monkeypatch.setattr(flow, "_write_report", lambda *_args: None)
    source = REPO / "scripts" / "ai_fix_agent.py"
    monkeypatch.setattr(
        flow.agent,
        "run_agent",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "diagnosis": "fallo reproducido",
            "fixes": [{"path": "apps/news/src/core/llm.py", "content": "x = 1\n"}],
            "env_fixes": [],
            "manual": [],
        },
    )
    result = flow.run_ai_fix(
        ctx,
        dry_run=True,
        assume_yes=True,
        emit=lambda _message: None,
        source_path=source,
    )
    assert result.status == "ok"
    assert result.applied == []
    assert result.agent_removed
    assert not ctx.envs["news"].path.exists()
