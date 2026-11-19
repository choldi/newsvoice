"""Build a secret-free setup report suitable for a GitHub issue."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .collections import COLLECTIONS_PATH, load_collections
from .detect import Report
from .envfile import EnvFile
from .settings import (
    ENGINE_INFO,
    Profile,
    backend_language_options,
    backend_voice_help,
    backend_voice_options,
    config_requires_gpu,
    configured_engines,
    fields_for,
    gateway_docker_extras,
    gateway_pip_extras,
    parse_engine_map,
    required_model_downloads,
)
from .util import REPO_ROOT
from .validate import Check

__all__ = ["REPORT_DIR", "REPORT_PATH", "build_config_report", "write_config_report"]

REPORT_DIR = REPO_ROOT / "reports"
REPORT_PATH = REPORT_DIR / "setup-report.md"

_SECRET_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|_HASH$|_KEY$|^KEY$|API_KEY|PRIVATE|CREDENTIAL)",
    re.IGNORECASE,
)
_PII_KEY = re.compile(
    r"(^|_)(CHAT_ID|USERNAME|SMTP_USER|EMAIL|PHONE|RECIPIENT|RECIPIENTS|ALLOWLIST|GROUP_ID)($|_)",
    re.IGNORECASE,
)
_PLACEHOLDER_MARKERS = ("your_", "example", "cambiar", "changeme", "<", ">")
_MAX_STRING = 4000


def _redact_url(value: str) -> str:
    """Remove ``user:password@`` credentials embedded in a URL."""
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", value)


def _truncate(value: str) -> str:
    """Cap very long free-form values so the report fits in an issue body."""
    if len(value) <= _MAX_STRING:
        return value
    return f"{value[:_MAX_STRING]}\n… <truncated {len(value) - _MAX_STRING} chars>"


def _mask_value(key: str, value: str, kind: str = "") -> str:
    """Return a safe representation of one configuration value."""
    if not value:
        return "<empty>"
    secret = kind in {"secret", "password"} or bool(_SECRET_KEY.search(key))
    if secret or _PII_KEY.search(key):
        if any(marker in value.lower() for marker in _PLACEHOLDER_MARKERS):
            return "<placeholder>"
        return f"<redacted ({len(value)} chars)>"
    return _redact_url(_truncate(value))


def _cell(value: Any) -> str:
    """Render one Markdown table cell without breaking the table."""
    text = str(value).replace("|", "\\|").replace("`", "'").replace("\n", " ")
    return f"`{text}`" if text else "<empty>"


def _display_path(path: Path) -> str:
    """Show repository-relative paths when possible, absolute paths otherwise."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sanitize_obj(value: Any, key: str = "") -> Any:
    """Recursively redact secrets and PII from parsed collection data."""
    if isinstance(value, dict):
        return {str(k): _sanitize_obj(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_obj(item, key) for item in value]
    if isinstance(value, str):
        if _SECRET_KEY.search(key) or _PII_KEY.search(key):
            return "<redacted>"
        return _truncate(_redact_url(value))
    return value


def _git_commit() -> str:
    """Return the short HEAD commit when the report is generated in a checkout."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _findings_table(report: Report) -> list[str]:
    """Render host detection findings."""
    lines = ["| Check | Status | Detail |", "|---|---|---|"]
    for finding in report.findings:
        lines.append(
            f"| {_cell(_redact_url(finding.name))} | {_cell(finding.status)} | "
            f"{_cell(_redact_url(_truncate(finding.detail)))} |"
        )
    return lines


def _checks_table(checks: list[Check]) -> list[str]:
    """Render validation checks."""
    lines = ["| Check | Status | Detail |", "|---|---|---|"]
    for check in checks:
        detail = _mask_value(check.name, check.detail)
        lines.append(
            f"| {_cell(_redact_url(check.name))} | {_cell(check.status)} | {_cell(detail)} |"
        )
    return lines


def _env_table(key: str, env: EnvFile) -> list[str]:
    """Render one env file with secret values masked."""
    fields = {field.key: field for field in fields_for(key)}
    lines = [
        f"### `{env.key_path}`",
        "",
        f"- Present: `{str(env.exists).lower()}`",
        f"- Loaded from template: `{str(env.from_example).lower()}`",
        "",
        "| Key | Value |",
        "|---|---|",
    ]
    seen: set[str] = set()
    for entry in env.entries():
        if entry.key is None or entry.commented or entry.key in seen:
            continue
        seen.add(entry.key)
        field = fields.get(entry.key)
        kind = field.kind if field else ""
        lines.append(
            f"| `{entry.key}` | {_cell(_mask_value(entry.key, env.get(entry.key), kind))} |"
        )
    if not seen:
        lines.append("| <none> | <no active keys> |")
    return lines


def _engine_table(engines: list[str]) -> list[str]:
    """Render the capabilities and infrastructure needs of configured engines."""
    lines = [
        "| Engine | Device | GPU scope | Profiles | Containers | Credentials | Terms | "
        "Cloning | Languages | Stock voices | Required models |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in engines:
        info = ENGINE_INFO.get(name)
        if info is None:
            lines.append(
                f"| `{name}` | `unknown` | `unknown` | `unknown` | `unknown` | "
                "`unknown` | `unknown` | `unknown` | `unknown` | `0` | `0` |"
            )
            continue
        languages = "all" if info.languages == ("*",) else ",".join(info.languages)
        profiles = ",".join(info.profiles) or "-"
        services = ",".join(info.services) or "-"
        keys = ",".join(info.required_keys) or "-"
        terms = "yes" if info.terms_required else "-"
        lines.append(
            f"| `{name}` | `{info.device}` | `{info.gpu_scope or '-'}` | `{profiles}` | "
            f"`{services}` | `{keys}` | `{terms}` | `{str(info.cloning).lower()}` | "
            f"`{languages}` | `{len(info.voices)}` | `{len(info.downloads)}` |"
        )
    return lines


def _voice_catalog_lines() -> list[str]:
    """Summarize the committed voice catalog without copying reference text."""
    path = REPO_ROOT / "apps" / "gateway" / "voice_profiles.json"
    if not path.exists():
        return ["- `voice_profiles.json`: not present"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"- `voice_profiles.json`: unreadable ({exc})"]
    profiles = data.get("profiles", []) if isinstance(data, dict) else []
    languages = data.get("languages", {}) if isinstance(data, dict) else {}
    lines = [
        f"- `voice_profiles.json`: `{len(profiles)}` profile(s), "
        f"languages: `{','.join(sorted(languages)) or 'none'}`",
        "",
        "| Name | Label | Language | Role | Enabled |",
        "|---|---|---|---|---|",
    ]
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        lines.append(
            f"| {_cell(profile.get('name', ''))} | {_cell(profile.get('label', ''))} | "
            f"{_cell(profile.get('language', ''))} | {_cell(profile.get('role_hint', ''))} | "
            f"{_cell(profile.get('enabled', ''))} |"
        )
    return lines


def _collections_lines() -> list[str]:
    """Render collections with recipient identifiers redacted."""
    lines = ["## Collections", ""]
    if not COLLECTIONS_PATH.exists():
        lines.append(f"- `{_display_path(COLLECTIONS_PATH)}`: not present")
        return lines
    data = load_collections(COLLECTIONS_PATH)
    if not data:
        lines.append(f"- `{_display_path(COLLECTIONS_PATH)}`: unreadable or empty")
        return lines
    defaults = data.get("collection_defaults", {})
    collections = data.get("collections", [])
    lines.append(f"- File: `{_display_path(COLLECTIONS_PATH)}`")
    lines.append(f"- Collections: `{len(collections)}`")
    if defaults:
        lines.extend(
            [
                "",
                "### Defaults",
                "",
                "```json",
                json.dumps(_sanitize_obj(defaults), ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    for item in collections:
        if not isinstance(item, dict):
            continue
        merged = {**defaults, **item}
        lines.extend(
            [
                "",
                f"### `{merged.get('name', '<unnamed>')}`",
                "",
                "```json",
                json.dumps(_sanitize_obj(merged), ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    return lines


def _tts_lines(envs: dict[str, EnvFile]) -> list[str]:
    """Render the derived TTS selection, voices and install extras."""
    gateway = envs.get("gateway")
    news = envs.get("news")
    if gateway is None or news is None:
        return ["## TTS", "", "- gateway/news env files are missing"]
    engine = gateway.get("TTS_ENGINE")
    engine_map = gateway.get("TTS_ENGINES")
    extra = gateway.get("TTS_EXTRA_ENGINES")
    engines = configured_engines(engine, engine_map, extra)
    routing = parse_engine_map(engine_map) or {}
    backend = news.get("TTS_DEFAULT_BACKEND")
    language = news.get("TTS_LANGUAGE")
    voice = news.get("TTS_VOICE")
    voices = backend_voice_options(backend, engine, engine_map, extra)
    languages = backend_language_options(backend, engine, engine_map, extra)
    gateway_url = _redact_url(news.get("TTS_API_BASE_URL"))
    omnivoice_url = _redact_url(gateway.get("OMNIVOICE_REMOTE_URL"))
    qwen3_url = _redact_url(gateway.get("QWEN3_TTS_REMOTE_URL"))
    lines = [
        "## TTS",
        "",
        f"- Gateway engine: `{engine or '<empty>'}`",
        f"- Routing: `{engine_map or '<empty>'}`",
        f"- Extra engines: `{extra or '<empty>'}`",
        f"- Configured engines: `{', '.join(engines) or '<none>'}`",
        f"- Requires GPU: `{str(config_requires_gpu(engine, engine_map, extra)).lower()}`",
        f"- Device: `{gateway.get('TTS_DEVICE') or '<empty>'}`",
        f"- Gateway URL (news): `{gateway_url or '<empty>'}`",
        f"- OmniVoice sidecar URL: `{omnivoice_url or '<empty>'}`",
        f"- qwen3 sidecar URL: `{qwen3_url or '<empty>'}`",
        f"- News backend: `{backend or '<empty>'}`",
        f"- News language: `{language or '<empty>'}`",
        f"- News voice: `{voice or '<empty>'}`",
        f"- Backend languages: `{', '.join(languages) or '<none>'}`",
        f"- Backend stock voices: `{', '.join(voices) or '<none>'}`",
        f"- Voice help: {backend_voice_help(backend, engine, engine_map, extra)}",
        "- Required model downloads: "
        f"`{', '.join(required_model_downloads(engine, engine_map, extra)) or '<none>'}`",
        "- Gateway pip extras: "
        f"`{', '.join(gateway_pip_extras(engine, engine_map, extra)) or '<none>'}`",
        "- Gateway Docker extras: "
        f"`{gateway_docker_extras(engine, engine_map, extra) or '<none>'}`",
    ]
    if routing:
        lines.append(
            "- Routing map: "
            + ", ".join(f"`{code}`→`{name}`" for code, name in sorted(routing.items()))
        )
    lines.extend(["", "### Configured engine capabilities", "", *_engine_table(engines)])
    return lines


def build_config_report(
    envs: dict[str, EnvFile],
    report: Report,
    *,
    profile: Profile,
    method: str,
    checks: list[Check],
    written: list[str] | tuple[str, ...] = (),
) -> str:
    """Return the complete Markdown report without any secret values."""
    generated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = _git_commit()
    lines = [
        "# newsvoice setup report",
        "",
        "> Generated by `python3 scripts/setup.py`.",
        "> Secrets are masked and credentials embedded in URLs are redacted.",
        "> Safe to attach to a GitHub issue.",
        "",
        "## Environment",
        "",
        f"- Generated: `{generated}`",
        f"- Repository: `{REPO_ROOT.name}`" + (f" (`{commit}`)" if commit else ""),
        f"- Profile: `{profile.key}` — {profile.label}",
        f"- Install method: `{method}`",
        f"- Python: `{report.python}`",
        f"- Platform: `{platform.system()} {platform.release()} {platform.machine()}`",
        f"- Files written: `{', '.join(written) or '<none>'}`",
        "",
        "## Host detection",
        "",
        *_findings_table(report),
        "",
        "## Validation",
        "",
        *_checks_table(checks),
        "",
        "## Configuration",
        "",
    ]
    for key in ("deploy", "news", "gateway"):
        env = envs.get(key)
        if env is None:
            continue
        lines.extend(_env_table(key, env))
        lines.append("")
    lines.extend(_tts_lines(envs))
    lines.extend(["", "## Voices", "", *_voice_catalog_lines()])
    lines.extend(["", *_collections_lines()])
    lines.extend(
        [
            "",
            "## Upload",
            "",
            "Create a GitHub issue with this report:",
            "",
            "```bash",
            "./scripts/upload_setup_report.sh",
            "```",
            "",
            "Or comment on an existing issue:",
            "",
            "```bash",
            "./scripts/upload_setup_report.sh --issue 123",
            "```",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_config_report(
    envs: dict[str, EnvFile],
    report: Report,
    *,
    profile: Profile,
    method: str,
    checks: list[Check],
    written: list[str] | tuple[str, ...] = (),
    path: Path = REPORT_PATH,
) -> Path:
    """Write the secret-free report and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_config_report(
            envs,
            report,
            profile=profile,
            method=method,
            checks=checks,
            written=written,
        ),
        encoding="utf-8",
    )
    return path
