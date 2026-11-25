"""Tests for automatic setup repairs shared by the TUI, line mode and CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from setup_wizard import __main__ as cli
from setup_wizard import repair
from setup_wizard import validate as validation
from setup_wizard.app import (
    Context,
    repair_screen,
    runtime_modes_screen,
    tts_screen,
    validate_screen,
)
from setup_wizard.curses_ui import App
from setup_wizard.detect import Report
from setup_wizard.envfile import EnvFile
from setup_wizard.settings import (
    PROFILES,
    parse_engine_map,
    parse_runtime_modes,
    split_multi,
)

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


def _checks(ctx: Context) -> list[validation.Check]:
    """Validate a context without touching the host."""
    return validation.validate(ctx.envs, ctx.report, will_write=False, profile=ctx.profile.key)


def test_duplicate_keys_are_commented_out(tmp_path: Path) -> None:
    """Only the first active copy of a repeated key survives a repair."""
    ctx = _context(tmp_path)
    gateway = ctx.envs["gateway"]
    gateway.lines.append("TTS_ENGINE=edgetts")

    fix = next(item for item in repair.all_fixes(ctx) if item.id == "duplicates:gateway")
    result = repair.apply_fixes(ctx, [fix])

    assert result.applied
    assert any(line.startswith("# TTS_ENGINE=edgetts") for line in gateway.lines)
    assert not any(check.name == "claves duplicadas" for check in _checks(ctx))


def test_canonical_repair_rewrites_selection_modes_and_routing(tmp_path: Path) -> None:
    """Legacy ids are replaced wherever the gateway references engines."""
    ctx = _context(tmp_path)
    gateway = ctx.envs["gateway"]
    gateway.set("TTS_ENGINE", "edge")
    gateway.set("TTS_ENGINES", '{"es":"edge"}')
    gateway.set("TTS_RUNTIME_MODES", '{"edge":"embedded"}')
    repair._sync_derived(ctx)

    fix = next(item for item in repair.conflict_fixes(ctx) if item.id == "canonical")
    repair.apply_fixes(ctx, [fix])

    assert gateway.get("TTS_ENGINE") == "edgetts"
    assert parse_engine_map(gateway.get("TTS_ENGINES")) == {"es": "edgetts"}
    assert parse_runtime_modes(gateway.get("TTS_RUNTIME_MODES")) == {"edgetts": "embedded"}


def test_invalid_engine_repair_falls_back_to_a_configurable_id(tmp_path: Path) -> None:
    """Proxies and non-selectable ids are removed from the gateway selection."""
    ctx = _context(tmp_path)
    gateway = ctx.envs["gateway"]
    gateway.set("TTS_ENGINE", "gateway")
    gateway.set("TTS_ENGINES", "")
    gateway.set("TTS_EXTRA_ENGINES", "gateway")
    gateway.set("TTS_RUNTIME_MODES", "")
    repair._sync_derived(ctx)

    fix = next(item for item in repair.conflict_fixes(ctx) if item.id == "invalid-ids")
    repair.apply_fixes(ctx, [fix])

    assert gateway.get("TTS_ENGINE") == "edgetts"
    assert "gateway" not in split_multi(gateway.get("TTS_EXTRA_ENGINES"))


def test_conflict_repair_removes_the_runtime_conflict(tmp_path: Path) -> None:
    """Two embedded engines that share the gateway become compatible again."""
    ctx = _context(tmp_path)
    gateway = ctx.envs["gateway"]
    gateway.set("TTS_ENGINE", "routed")
    gateway.set("TTS_ENGINES", '{"es":"kokoro"}')
    gateway.set("TTS_EXTRA_ENGINES", "qwen3_tts")
    gateway.set("TTS_RUNTIME_MODES", '{"kokoro":"embedded","qwen3_tts":"embedded"}')
    repair._sync_derived(ctx)

    assert any(
        check.name == "conflictos de runtime" and check.status == "bad" for check in _checks(ctx)
    )
    fix = next(item for item in repair.conflict_fixes(ctx) if item.id.startswith("conflict:"))
    result = repair.apply_fixes(ctx, [fix])

    assert result.applied
    assert not any(
        check.name == "conflictos de runtime" and check.status == "bad" for check in _checks(ctx)
    )


def test_secret_repair_unifies_the_gateway_key_and_generates_tokens(tmp_path: Path) -> None:
    """The three gateway-key copies and internal tokens are repaired together."""
    ctx = _context(tmp_path)
    ctx.envs["deploy"].set("GATEWAY_API_KEY", "shared-key")
    ctx.envs["news"].set("TTS_API_KEY", "different-key")
    ctx.envs["news"].set("ADMIN_API_TOKEN", "")
    ctx.envs["news"].set("DOC_TTS_LINK_SECRET", "")

    fix = next(item for item in repair.all_fixes(ctx) if item.id == "secrets")
    repair.apply_fixes(ctx, [fix])

    assert ctx.envs["gateway"].get("API_KEY") == "shared-key"
    assert ctx.envs["news"].get("TTS_API_KEY") == "shared-key"
    assert ctx.envs["news"].get("ADMIN_API_TOKEN")
    assert ctx.envs["news"].get("DOC_TTS_LINK_SECRET")
    assert not any("enlaces firmados" in item for item in repair.manual_issues(ctx))


def test_all_fixes_never_accepts_model_licenses(tmp_path: Path) -> None:
    """License terms stay manual even when every automatable fix is requested."""
    ctx = _context(tmp_path)
    ctx.envs["gateway"].set("TTS_ENGINE", "chatterbox")
    ctx.envs["gateway"].set("TTS_ENGINES", "")
    ctx.envs["deploy"].set("ENGINE_TERMS_ACCEPTED", "")
    repair._sync_derived(ctx)

    fixes = repair.all_fixes(ctx)

    assert not any(fix.id == "terms" for fix in fixes)
    assert "chatterbox" not in split_multi(ctx.envs["deploy"].get("ENGINE_TERMS_ACCEPTED"))
    assert any("términos engines" in item for item in repair.manual_issues(ctx))


def test_repair_screen_applies_every_visible_fix(tmp_path: Path) -> None:
    """The TUI repair screen applies staged fixes and records them."""
    ctx = _context(tmp_path)
    ctx.envs["gateway"].set("TTS_ENGINE", "edge")
    ctx.envs["gateway"].set("TTS_ENGINES", "")
    app = App("test")
    screen = repair_screen(app, ctx, all_problems=False, on_done=lambda _app: None)
    app.push(screen)
    button = next(row for row in screen.rows if row.action == "fix_all")

    assert screen.on_activate is not None
    screen.on_activate(app, button)

    assert ctx.envs["gateway"].get("TTS_ENGINE") == "edgetts"
    assert ctx.repairs


def test_engine_screens_offer_the_conflict_repair_shortcut(tmp_path: Path) -> None:
    """Both engine editors expose the same repair screen."""
    ctx = _context(tmp_path)
    app = App("test")

    assert any(row.action == "fix_conflicts" for row in tts_screen(app, ctx).rows)
    assert any(row.action == "fix_conflicts" for row in runtime_modes_screen(app, ctx).rows)


def test_validation_screen_offers_solve_everything(tmp_path: Path) -> None:
    """The final validation screen exposes the all-problems repair."""
    ctx = _context(tmp_path)
    app = App("test")
    app.push(validate_screen(app, ctx, write_mode=True))

    assert any(row.action == "fix_all" for row in app.current.rows)


def test_report_includes_repairs_and_manual_items(tmp_path: Path) -> None:
    """The generated report records what was fixed and what remains manual."""
    ctx = _context(tmp_path)
    gateway = ctx.envs["gateway"]
    gateway.set("TTS_ENGINE", "edge")
    repair._sync_derived(ctx)
    fix = next(item for item in repair.conflict_fixes(ctx) if item.id == "canonical")
    result = repair.apply_fixes(ctx, [fix])
    target = tmp_path / "setup-report.md"

    from setup_wizard.config_report import write_config_report

    write_config_report(
        ctx.envs,
        ctx.report,
        profile=PROFILES[0],
        method="skip",
        checks=_checks(ctx),
        repairs=[item for item in result.applied],
        manual=["telegram token"],
        path=target,
    )
    text = target.read_text(encoding="utf-8")

    assert "## Repairs" in text
    assert "Canonicalizar IDs de engines" in text
    assert "### Still manual" in text
    assert "telegram token" in text


def test_cli_fix_all_writes_the_env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI repair path applies staged fixes and persists them."""
    ctx = _context(tmp_path)
    ctx.envs["gateway"].set("TTS_ENGINE", "edge")
    ctx.envs["gateway"].set("TTS_ENGINES", "")
    report_path = tmp_path / "setup-report.md"
    monkeypatch.setattr(cli, "load_context", lambda: ctx)
    monkeypatch.setattr(
        cli,
        "write_config_report",
        lambda *_args, **_kwargs: report_path,
    )

    assert cli.main(["--fix-all", "--yes"]) in (0, 1)

    assert "TTS_ENGINE=edgetts" in (tmp_path / "apps/gateway/.env").read_text(encoding="utf-8")
    assert ctx.repairs


def test_cli_fix_flags_are_mutually_exclusive() -> None:
    """The CLI rejects a combined conflict-only and full repair request."""
    with pytest.raises(SystemExit):
        cli.main(["--fix-conflicts", "--fix-all"])
