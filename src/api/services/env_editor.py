"""Read, validate and rewrite the ``.env`` file (GUI settings editor).

The schema is **derived from the pydantic ``Settings`` model**, so the editor
cannot drift from the code: env names, types, ranges and defaults all come from
`src.core.config`. The rules are:

- validation happens by constructing a throwaway ``Settings`` with the
  submitted values (one source of truth for types and ``ge``/``le`` ranges);
- nothing is written unless **every** field validates ("grabando si está
  conforme"); a failed POST leaves the file byte-for-byte untouched;
- only changed keys are rewritten, comments and ordering are preserved, and
  the previous file is copied to ``.env.bak`` first;
- secrets are never rendered back to the browser: an empty secret input means
  "keep the current value".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import ValidationError

from src.api.services.audio_files import atomic_write
from src.core.config import Settings

#: ``env_NAME`` inputs that map to an unbounded string but actually accept a
#: short list of values (the model types them as plain ``str``).
EXTRA_RULES: dict[str, set[str]] = {
    "LLM_VERIFY_BACKEND": {"local", "modal", "api"},
    "LOG_LEVEL": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
    "WHATSAPP_DELAY_MODE": {"off", "natural", "custom"},
    "WHATSAPP_TYPING_INDICATOR": {"composing", "none"},
    "AUDIO_VERIFY_DEVICE": {"cpu", "cuda", "auto"},
}

#: Prefix -> section title, matched longest-prefix-first so ``llm_verify_`` wins
#: over ``llm_``. Fields with no match land in "General".
SECTIONS: tuple[tuple[str, str], ...] = (
    ("llm_verify_", "Verificación LLM (7.13)"),
    ("audio_verify_", "Verificación de audio (7.14)"),
    ("doc_tts_", "TTS de documentos (7.11)"),
    ("teacher_", "Profesor de idiomas (7.12)"),
    ("url_summary_", "Resumen de URL (7.10)"),
    ("url_fetch_", "Resumen de URL (7.10)"),
    ("source_reliability_", "Fiabilidad de fuentes"),
    ("min_source_", "Fiabilidad de fuentes"),
    ("whatsapp_", "WhatsApp"),
    ("smtp_", "Email"),
    ("mail_", "Email"),
    ("telegram_", "Telegram"),
    ("narration_", "Narración"),
    ("tts_", "TTS del digest"),
    ("monitor_", "Monitorización"),
    ("collections_", "Colecciones"),
    ("rss_", "Colecciones"),
    ("voice_", "Voces"),
    ("temporal_", "Temporal"),
    ("modal_", "Modal"),
    ("admin_", "Admin / GUI"),
    ("api_", "API"),
    ("log_", "API"),
    ("database_", "API"),
    ("llm_", "LLM del digest"),
)

#: Secret-looking field names; their values never reach the HTML. Counters
#: (LLM_MAX_TOKENS, QWEN_TTS_MAX_NEW_TOKENS) match "token" but are numbers, so
#: the second pattern exempts them.
_SECRET_RE = re.compile(r"(token|secret|password|hash|_key$)", re.IGNORECASE)
_NON_SECRET_RE = re.compile(r"(max|count|size|limit)", re.IGNORECASE)


@dataclass(frozen=True)
class SettingField:
    """One editable setting derived from ``Settings.model_fields``."""

    env_name: str
    name: str
    annotation: Any
    default: Any
    ge: float | None
    le: float | None
    is_secret: bool
    section: str
    description: str

    @property
    def hint(self) -> str:
        """Return a human hint about type/range/default for the form.

        Returns:
            Text like 'entero · 0–1 · por defecto 4096' (may be empty).
        """
        parts: list[str] = []
        if self.ge is not None or self.le is not None:
            lo = "-inf" if self.ge is None else self.ge
            hi = "+inf" if self.le is None else self.le
            parts.append(f"{lo} – {hi}")
        if self.default not in (None, ""):
            parts.append(f"por defecto {to_env_value(self.default)}")
        return " · ".join(parts)


@dataclass
class EnvEntry:
    """A ``KEY=VALUE`` line of the .env file."""

    key: str
    value: str
    line_index: int
    inline_comment: str = ""


@dataclass
class EnvDocument:
    """A parsed .env file: raw lines plus an index of its assignments."""

    lines: list[str] = field(default_factory=list)
    entries: dict[str, EnvEntry] = field(default_factory=dict)


def env_name_of(name: str) -> str:
    """Return the environment variable name of a Settings field.

    Args:
        name: Field name (``llm_model``).

    Returns:
        The env name (``LLM_MODEL``); pydantic-settings' default mapping.
    """
    return name.upper()


def _section_for(env_name: str) -> str:
    """Return the section title for an env name (longest prefix wins).

    Args:
        env_name: Environment variable name.

    Returns:
        The section title.
    """
    lowered = env_name.lower()
    for prefix, title in sorted(SECTIONS, key=lambda item: -len(item[0])):
        if lowered.startswith(prefix):
            return title
    return "General"


def _constraint(annotation: Any, field_info: Any) -> tuple[float | None, float | None]:
    """Extract (ge, le) from a pydantic field's annotated constraints.

    Args:
        annotation: The field annotation (unused, kept for symmetry).
        field_info: The FieldInfo from ``Settings.model_fields``.

    Returns:
        Tuple of (minimum, maximum), each None when not constrained.
    """
    ge: float | None = None
    le: float | None = None
    for constraint in getattr(field_info, "metadata", ()):
        for attr, target in (("ge", "ge"), ("gt", "gt"), ("le", "le"), ("lt", "lt")):
            value = getattr(constraint, attr, None)
            if value is None:
                continue
            if attr == "gt":
                ge = float(value)
            elif attr == "lt":
                le = float(value)
            elif target == "ge":
                ge = float(value)
            else:
                le = float(value)
    return ge, le


def build_schema() -> list[SettingField]:
    """Build the editable schema from the pydantic Settings model.

    Returns:
        One SettingField per model field, in declaration order.
    """
    fields: list[SettingField] = []
    for name, field_info in Settings.model_fields.items():
        ge, le = _constraint(field_info.annotation, field_info)
        try:
            # default_factory fields (monitor_notify_channels, ...) expose only
            # PydanticUndefined; evaluate the factory for the hint.
            default = field_info.get_default(call_default_factory=True)
        except TypeError:
            default = None
        fields.append(
            SettingField(
                env_name=env_name_of(name),
                name=name,
                annotation=field_info.annotation,
                default=default,
                ge=ge,
                le=le,
                is_secret=bool(_SECRET_RE.search(name)) and not bool(_NON_SECRET_RE.search(name)),
                section=_section_for(env_name_of(name)),
                description=(field_info.description or "").strip(),
            )
        )
    return fields


def to_env_value(value: Any) -> str:
    """Serialize a Settings value into its .env representation.

    Args:
        value: A typed settings value.

    Returns:
        The string form written to .env (JSON for lists/dicts, as pydantic-
        settings parses complex values from JSON).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def is_complex(annotation: Any) -> bool:
    """Return True when the field is parsed from JSON (list/dict).

    Args:
        annotation: The field annotation.

    Returns:
        True for list/dict/Json-shaped annotations.
    """
    origin = get_origin(annotation)
    if origin in (list, dict):
        return True
    return (
        getattr(annotation, "__name__", "") == "Json"
        or bool(get_args(annotation))
        and (getattr(get_args(annotation)[0], "__name__", "") in ("list", "dict"))
    )


def parse_env(text: str) -> EnvDocument:
    """Parse a .env file, preserving every line for the rewrite.

    Handles ``KEY=value``, quoted values and inline comments (``KEY=v  # note``)
    the way python-dotenv does: an unquoted ``#`` after whitespace starts a
    comment.

    Args:
        text: Raw file contents.

    Returns:
        The document with lines and an assignment index.
    """
    doc = EnvDocument(lines=text.splitlines())
    for index, line in enumerate(doc.lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = raw_value
        inline_comment = ""
        comment_pos = _find_inline_comment(raw_value)
        if comment_pos != -1:
            inline_comment = raw_value[comment_pos:].strip()
            value = raw_value[:comment_pos]
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        doc.entries[key] = EnvEntry(
            key=key, value=value, line_index=index, inline_comment=inline_comment
        )
    return doc


def _find_inline_comment(raw_value: str) -> int:
    """Locate an inline ``#`` comment outside of quotes.

    Args:
        raw_value: Everything after ``KEY=``.

    Returns:
        Index of the comment (including its leading space), or -1.
    """
    quote: str | None = None
    for index, char in enumerate(raw_value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or raw_value[index - 1].isspace()):
            return index
    return -1


def format_line(key: str, value: str, inline_comment: str = "") -> str:
    """Render one ``KEY=VALUE`` line, quoting only when TOML/dotenv needs it.

    Args:
        key: Variable name.
        value: Raw value as typed by the operator.
        inline_comment: Comment to keep after the value ('' to drop it).

    Returns:
        A single .env line.
    """
    needs_quotes = (
        not value
        or value != value.strip()
        or any(char in value for char in ("#", "\n", "\t"))
        or value[:1] in ("'", '"')
    )
    rendered = f'"{value}"' if needs_quotes else value
    line = f"{key}={rendered}"
    if inline_comment:
        line = f"{line}  {inline_comment}"
    return line


@dataclass
class ValidationProblem:
    """A rejected value with a message for the form."""

    env_name: str
    message: str


def effective_values(fields: list[SettingField]) -> dict[str, str]:
    """Return the current value of every field in env representation.

    Args:
        fields: The schema (from :func:`build_schema`).

    Returns:
        Mapping env name -> current string value ('' for unset secrets).
    """
    # The file wins over the runtime settings: this editor edits the file, so
    # its content is what the form must show. Keys absent from the file fall
    # back to the runtime value (covers vars injected by compose `environment:`).
    if ENV_FILE.exists():
        document = parse_env(ENV_FILE.read_text(encoding="utf-8"))
    else:
        document = EnvDocument()
    settings = Settings()
    result: dict[str, str] = {}
    for spec in fields:
        entry = document.entries.get(spec.env_name)
        raw = entry.value if entry is not None else getattr(settings, spec.name, None)
        # Secrets are included: the value feeds the "configured" flag; only
        # `form_context` decides what is rendered, and it never echoes one.
        result[spec.env_name] = to_env_value(raw)
    return result


def validate_changes(
    fields: list[SettingField], changes: dict[str, str]
) -> tuple[dict[str, str], list[ValidationProblem]]:
    """Validate every submitted change through the real Settings model.

    Args:
        fields: The schema.
        changes: env name -> raw user input (only fields the user edited).

    Returns:
        Tuple of (clean values keyed by FIELD name for the Settings call,
        problems found). Empty problems means every value is acceptable.
    """
    by_env = {spec.env_name: spec for spec in fields}
    problems: list[ValidationProblem] = []
    for env_name, raw in changes.items():
        spec = by_env.get(env_name)
        if spec is None:
            # Not part of the schema: ignore rather than persist a stray key.
            continue
        allowed = EXTRA_RULES.get(env_name)
        if allowed is not None and raw.strip() not in allowed:
            problems.append(
                ValidationProblem(
                    env_name,
                    f"valor no permitido {raw.strip()!r}: usa uno de {sorted(allowed)}",
                )
            )
    if problems:
        return {}, problems

    current = effective_values(fields)
    candidate: dict[str, str] = dict(current)
    candidate.update({env_name: raw for env_name, raw in changes.items() if env_name in by_env})
    # Secrets the user left blank keep their stored value.
    for spec in fields:
        if spec.is_secret and not candidate.get(spec.env_name):
            candidate[spec.env_name] = current.get(spec.env_name, "")

    kwargs: dict[str, Any] = {}
    for env, value in candidate.items():
        spec = by_env.get(env)
        if spec is None:
            continue
        parsed: Any = value
        if is_complex(spec.annotation) and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                problems.append(
                    ValidationProblem(env, 'JSON inválido (usa comillas dobles, p. ej. ["a"])')
                )
                continue
        kwargs[spec.name] = parsed
    if problems:
        return {}, problems
    try:
        Settings(_env_file=None, **kwargs)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            target = by_env.get(env_name_of(location)) or by_env.get(location)
            if target is None:
                continue
            problems.append(
                ValidationProblem(target.env_name, _translate(error.get("msg", "inválido")))
            )
    return kwargs, problems


def _translate(message: str) -> str:
    """Turn a pydantic message into a short Spanish hint.

    Args:
        message: Raw pydantic error message.

    Returns:
        A friendlier message (the raw one when nothing matches).
    """
    lowered = message.lower()
    if "greater than or equal" in lowered:
        return "por debajo del mínimo permitido"
    if "less than or equal" in lowered:
        return "por encima del máximo permitido"
    if "input should be a valid" in lowered or "valid " in lowered:
        return "tipo inválido para esta variable"
    if "json" in lowered:
        return 'JSON inválido (usa comillas dobles, p. ej. ["a"])'
    return message


def compute_changes(
    fields: list[SettingField],
    submitted: dict[str, str],
    current: dict[str, str],
) -> dict[str, str]:
    """Extract only the fields the operator actually edited.

    Args:
        fields: The schema.
        submitted: env name -> raw input for every ``env_*`` field posted.
        current: The effective values (from :func:`effective_values`).

    Returns:
        Mapping of the env names whose value differs from the displayed one.
        Secrets only count as a change when non-empty (blank = keep).
    """
    by_env = {spec.env_name: spec for spec in fields}
    changes: dict[str, str] = {}
    for env_name, raw in submitted.items():
        spec = by_env.get(env_name)
        if spec is None:
            continue
        value = raw.strip() if isinstance(raw, str) else str(raw)
        if spec.is_secret:
            if value:
                changes[env_name] = value
            continue
        if value == current.get(env_name, "").strip():
            continue
        changes[env_name] = value
    return changes


def apply_changes(document: EnvDocument, changes: dict[str, str]) -> str:
    """Return the new .env text with only the changed keys rewritten.

    Existing keys are updated in place (keeping their inline comment); unknown
    keys are appended at the end of the file under a marker comment.

    Args:
        document: The parsed current file.
        changes: env name -> new raw value.

    Returns:
        The new file contents (no trailing newline guarantees beyond the
        original lines).
    """
    lines = list(document.lines)
    appended = False
    for key, value in changes.items():
        entry = document.entries.get(key)
        if entry is not None:
            lines[entry.line_index] = format_line(key, value, entry.inline_comment)
            continue
        if not appended:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("# añadido por el editor de configuración")
            appended = True
        lines.append(format_line(key, value))
    return "\n".join(lines) + "\n"


def save_env(path: Path, changes: dict[str, str]) -> None:
    """Write the validated changes atomically, keeping a ``.env.bak`` copy.

    Args:
        path: The .env file.
        changes: env name -> new value (assumed already validated).

    Raises:
        OSError: When the file cannot be read or written.
    """
    original = path.read_text(encoding="utf-8")
    document = parse_env(original)
    updated = apply_changes(document, changes)
    backup = path.with_suffix(".env.bak") if path.suffix == ".env" else path.parent / ".env.bak"
    try:
        backup.write_text(original, encoding="utf-8")
    except OSError:
        # A missing backup must never block the save.
        pass
    atomic_write(path, updated.encode("utf-8"))


#: Where the settings come from; mirrors ``Settings.model_config['env_file']``.
ENV_FILE = Path(getattr(Settings, "model_config", {}).get("env_file", ".env"))


def form_context(
    fields: list[SettingField],
    *,
    submitted: dict[str, str] | None = None,
    problems: list[ValidationProblem] | None = None,
) -> list[dict]:
    """Group the schema into sections ready for the settings template.

    Secrets are never rendered back: their input is always empty, with a
    placeholder that says whether one is stored. On a rejected POST the
    operator's own input is echoed for every non-secret field.

    Args:
        fields: The schema (from :func:`build_schema`).
        submitted: Raw ``env_*`` values from a failed POST, if any.
        problems: Validation problems of that POST, if any.

    Returns:
        List of {"name": str, "fields": [...]}, in declaration order. Each
        field carries env_name, value, placeholder, is_secret, configured,
        hint, description and error.
    """
    current = effective_values(fields)
    errors = {problem.env_name: problem.message for problem in (problems or [])}
    submitted = submitted or {}
    sections: dict[str, list[dict]] = {}
    for spec in fields:
        raw = submitted.get(spec.env_name)
        if spec.is_secret:
            configured = bool(current.get(spec.env_name))
            if raw:
                placeholder = "se guardará el valor tecleado"
            elif configured:
                placeholder = "•••• guardado — deja vacío para conservar"
            else:
                placeholder = "vacío"
            value = ""
        else:
            value = current.get(spec.env_name, "") if raw is None else raw
            placeholder = ""
        sections.setdefault(spec.section, []).append(
            {
                "env_name": spec.env_name,
                "value": value,
                "placeholder": placeholder,
                "is_secret": spec.is_secret,
                "configured": bool(current.get(spec.env_name)),
                "hint": spec.hint,
                "description": spec.description,
                "error": errors.get(spec.env_name, ""),
            }
        )
    return [{"name": name, "fields": rows} for name, rows in sections.items()]


def saved_flash(changes: dict[str, str]) -> str:
    """Build the success message, including the restart command.

    Args:
        changes: The env names that were written.

    Returns:
        Flash text with the restart instructions.
    """
    names = ", ".join(sorted(changes))
    return (
        f"Guardado ({len(changes)} cambio(s)): {names}. "
        f"Los settings viven cacheados en cada proceso: reinicia con "
        f"'docker compose restart api worker narration-worker doc-tts-worker "
        f"teacher-worker voice-catalog-worker'"
    )
