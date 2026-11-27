"""Declarative creation of the basic designed voices from the setup wizard."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .util import REPO_ROOT

BASIC_VOICES_PATH = REPO_ROOT / "config" / "basic_voices.toml"
DEFAULT_GATEWAY_URL = "http://localhost:8001/api/v1"
DEFAULT_NEWS_URL = "http://localhost:8000"
_DEFAULT_DESIGN_ENGINE = "qwen3_tts"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

__all__ = [
    "BASIC_VOICES_PATH",
    "BasicVoice",
    "VoiceCreationOptions",
    "VoiceCreationResult",
    "create_basic_voices",
    "load_basic_voices",
    "preview_basic_voices",
]


@dataclass(frozen=True)
class BasicVoice:
    """One declarative voice preset shared by the gateway and news flows."""

    name: str
    language: str
    instruct: str
    label: str = ""
    ref_text: str = ""
    role_hint: str = ""
    ref_segments: tuple[str, ...] = ()

    @property
    def display_label(self) -> str:
        """Return the human label, falling back to the stable slug."""
        return self.label or self.name


@dataclass(frozen=True)
class VoiceCreationOptions:
    """Connection details and behaviour of one creation run."""

    gateway_url: str = DEFAULT_GATEWAY_URL
    news_url: str = DEFAULT_NEWS_URL
    design_engine: str = _DEFAULT_DESIGN_ENGINE
    gateway_key: str = ""
    admin_token: str = ""
    dry_run: bool = False
    timeout: float = 30.0
    voices_path: Path = field(default_factory=lambda: BASIC_VOICES_PATH)

    def with_overrides(self, **changes: Any) -> VoiceCreationOptions:
        """Return a copy with only the supplied non-empty overrides."""
        values = {key: value for key, value in changes.items() if value not in (None, "", False)}
        return replace(self, **values) if values else self


@dataclass(frozen=True)
class VoiceCreationResult:
    """Outcome of both creation flows for one declarative voice."""

    voice: BasicVoice
    gateway: str
    news: str


def load_basic_voices(path: Path | str | None = None) -> list[BasicVoice]:
    """Read and validate ``config/basic_voices.toml``.

    Args:
        path: Manifest path. Defaults to the repository manifest.

    Returns:
        Validated voice presets in manifest order.

    Raises:
        ValueError: When the manifest shape or one entry is invalid.
    """
    target = Path(path or BASIC_VOICES_PATH)
    if not target.is_file():
        raise ValueError(f"No existe el archivo de voces básicas: {target}")
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    raw_voices = data.get("voices") or data.get("basic_voices") or []
    if not isinstance(raw_voices, list) or not raw_voices:
        raise ValueError(f"{target} debe declarar una lista `[[voices]]` no vacía")
    voices: list[BasicVoice] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_voices, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"La voz {index} de {target} debe ser una tabla TOML")
        name = str(item.get("name") or "").strip()
        if not _SLUG.match(name):
            raise ValueError(f"Nombre de voz inválido en la entrada {index}: {name!r}")
        if name in seen:
            raise ValueError(f"Nombre de voz duplicado: {name}")
        seen.add(name)
        language = str(item.get("language") or "").strip().lower()
        if len(language) != 2 or not language.isalpha():
            raise ValueError(f"Idioma inválido para {name}: {language!r}")
        instruct = str(item.get("instruct") or "").strip()
        if len(instruct) < 10:
            raise ValueError(f"`instruct` demasiado corto para {name}")
        ref_text = str(item.get("ref_text") or "").strip()
        raw_segments = item.get("ref_segments") or []
        segments = tuple(str(segment).strip() for segment in raw_segments if str(segment).strip())
        if not ref_text and not segments:
            raise ValueError(f"{name} necesita `ref_text` o `ref_segments`")
        voices.append(
            BasicVoice(
                name=name,
                language=language,
                instruct=instruct,
                label=str(item.get("label") or "").strip(),
                ref_text=ref_text,
                role_hint=str(item.get("role_hint") or "").strip(),
                ref_segments=segments,
            )
        )
    return voices


def preview_basic_voices(voices: list[BasicVoice], options: VoiceCreationOptions) -> list[str]:
    """Render the manifest and the endpoints that would be called."""
    lines = [
        f"Modo: {'simulación' if options.dry_run else 'creación'}",
        f"Gateway: {options.gateway_url} · engine: {options.design_engine}",
        f"News: {options.news_url} · admin: {'sí' if options.admin_token else 'no'}",
        "",
    ]
    for voice in voices:
        lines.append(
            f"{voice.name} · {voice.language} · {voice.display_label} · "
            f"rol={voice.role_hint or '-'}"
        )
        lines.append(f"  instruct: {voice.instruct}")
        lines.append(f"  ref_text: {voice.ref_text or ' / '.join(voice.ref_segments)}")
    return lines


def _headers(token: str) -> dict[str, str]:
    """Build the JSON headers with the optional gateway/admin secret."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["X-API-Key"] = token
        headers["X-Admin-Token"] = token
    return headers


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = 30.0,
) -> Any:
    """Issue one JSON request with stdlib urllib."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers=_headers(token))
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _error_detail(exc: HTTPError) -> str:
    """Extract a readable API error without dumping headers."""
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:200] or f"HTTP {exc.code}"
    detail = payload.get("detail", payload)
    return f"HTTP {exc.code}: {detail}"[:300]


def _existing_gateway_names(options: VoiceCreationOptions) -> set[str]:
    """Read the gateway designed-presets already created."""
    url = f"{options.gateway_url.rstrip('/')}/voices/designed?limit=1000"
    data = _request(url, token=options.gateway_key, timeout=options.timeout)
    if not isinstance(data, list):
        return set()
    return {str(item.get("name") or "") for item in data if isinstance(item, dict)}


def _existing_news_names(options: VoiceCreationOptions) -> set[str]:
    """Read the news generated voices already registered."""
    url = f"{options.news_url.rstrip('/')}/api/admin/voices/all?category=generated"
    data = _request(url, token=options.admin_token, timeout=options.timeout)
    voices = data.get("voices") if isinstance(data, dict) else None
    if not isinstance(voices, list):
        return set()
    return {str(item.get("id") or "") for item in voices if isinstance(item, dict)}


def _create_gateway_preset(voice: BasicVoice, options: VoiceCreationOptions) -> str:
    """Create one designed preset in the gateway."""
    url = f"{options.gateway_url.rstrip('/')}/voices/design"
    payload = {
        "name": voice.name,
        "instruct": voice.instruct,
        "language": voice.language,
        "engine": options.design_engine,
    }
    try:
        data = _request(
            url,
            method="POST",
            payload=payload,
            token=options.gateway_key,
            timeout=options.timeout,
        )
    except HTTPError as exc:
        return f"error: {_error_detail(exc)}"
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return f"error: {exc}"
    voice_id = str(data.get("voice_id") or data.get("id") or "") if isinstance(data, dict) else ""
    return f"creada{'' if not voice_id else f' ({voice_id})'}"


def _create_news_voice(voice: BasicVoice, options: VoiceCreationOptions) -> str:
    """Create one generated custom voice in news-engine."""
    url = f"{options.news_url.rstrip('/')}/api/admin/voices/custom"
    payload = {
        "name": voice.name,
        "label": voice.display_label,
        "language": voice.language,
        "instruct": voice.instruct,
        "ref_text": voice.ref_text,
        "ref_segments": list(voice.ref_segments),
        "role_hint": voice.role_hint,
        "generate": True,
        "upload": False,
        "run_in_modal": False,
    }
    try:
        data = _request(
            url,
            method="POST",
            payload=payload,
            token=options.admin_token,
            timeout=options.timeout,
        )
    except HTTPError as exc:
        return f"error: {_error_detail(exc)}"
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return f"error: {exc}"
    if not isinstance(data, dict):
        return "creada"
    workflow_id = str(data.get("workflow_id") or "")
    start_error = str(data.get("start_error") or "")
    if start_error:
        return f"perfil guardado, sin workflow: {start_error}"
    return f"creada{'' if not workflow_id else f' ({workflow_id})'}"


def create_basic_voices(
    options: VoiceCreationOptions, voices: list[BasicVoice] | None = None
) -> list[VoiceCreationResult]:
    """Create every declarative voice in both backends, idempotently.

    Args:
        options: Endpoints, credentials and behaviour of the run.
        voices: Parsed manifest; loaded from ``options.voices_path`` when omitted.

    Returns:
        One result per voice, with an independent status for each flow.
    """
    selected = voices if voices is not None else load_basic_voices(options.voices_path)
    gateway_names: set[str] = set()
    news_names: set[str] = set()
    if not options.dry_run:
        try:
            gateway_names = _existing_gateway_names(options)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            gateway_names = set()
        try:
            news_names = _existing_news_names(options)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            news_names = set()

    results: list[VoiceCreationResult] = []
    for voice in selected:
        if options.dry_run:
            gateway_status = "simulada"
            news_status = "simulada"
        else:
            gateway_status = (
                "ya existe"
                if voice.name in gateway_names
                else _create_gateway_preset(voice, options)
            )
            news_status = (
                "ya existe" if voice.name in news_names else _create_news_voice(voice, options)
            )
        results.append(VoiceCreationResult(voice=voice, gateway=gateway_status, news=news_status))
    return results
