"""Admin web GUI routes (Jinja2 + HTMX) for Phase 6.

Self-hosted single-page-ish admin console served under /ui. The GUI renders
collections, schedule calendar, executions (merged Temporal + DB runs), and
voice management. It uses the admin REST services directly (no HTTP hop) and
HTMX for live partial refreshes (executions polling, calendar, tables).

Auth is handled by the role-guard middleware (`src/api/auth.py`): admin
credentials for the local account, plus Phase 6 self-registration and
one-time email codes for non-admin users.

Plain form bodies are parsed with the stdlib (`urllib.parse.parse_qsl`);
multipart forms (the Phase 7.11/7.12 tool uploads) go through Starlette's
`request.form()`, which needs `python-multipart` — a declared runtime dependency
since Phase 7.11.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from src.api.authz import ASSIGNABLE_ROLES, Role
from src.api.db.database import (
    AudioJobsRepository,
    RunsRepository,
    UsersRepository,
    VerificationsRepository,
)
from src.api.models.lesson import LessonMode
from src.api.models.narration import NarrationSpec, TTSBackend
from src.api.models.phonetic import PhoneticHeuristicsIn, PhoneticHeuristicsOut
from src.api.models.voices import CustomVoiceIn
from src.api.services import (
    env_editor,
    phonetic_heuristics,
    voice_catalog_admin,
    voice_preview,
    voice_search,
)
from src.api.services.audio_cleanup import delete_job_artifacts
from src.api.services.collections_repo import CollectionConflictError, CollectionsRepository
from src.api.services.doc_tts import read_job_meta, submit_document
from src.api.services.env_editor import build_schema
from src.api.services.lesson import (
    LessonError,
    lesson_audio_link,
    read_lesson_meta,
    submit_lesson,
)
from src.api.services.phonetic_heuristics import HeuristicsError
from src.api.services.signed_links import (
    NARRATION_AUDIO_LINK_PREFIX,
    SignedLinkError,
    build_signed_path,
)
from src.api.services.submit import SubmitError
from src.api.services.temporal_admin import TemporalAdminService
from src.api.services.tts_estimate import eta_seconds
from src.api.services.voice_engine import engines_for_voice
from src.api.services.voice_registry import (
    edge_options,
    gateway_options,
    list_all_voices,
    modal_options,
)
from src.api.services.voices import VoicesService
from src.core import logging as logging_ext
from src.core import tracing
from src.core.collections import LogicDeliveryConfig
from src.core.prompts import EMOTION_TAGS

#: Level names offered by the observability UI, ordered low to high.
LOG_LEVEL_NAMES: tuple[str, ...] = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ui"])

templates = Jinja2Templates(directory="src/api/templates")

WEEKDAY_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


async def _form(request: Request) -> dict[str, str]:
    """Parse an `application/x-www-form-urlencoded` body into a string dict.

    Uses the stdlib to avoid a python-multipart runtime dependency. Checkbox
    fields absent from the request simply won't appear as keys.

    Args:
        request: Incoming request.

    Returns:
        Dict of form field name to its first string value.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for key, value in parse_qsl(raw, keep_blank_values=True):
        fields.setdefault(key, value)
    return fields


async def _file_form(request: Request) -> tuple[dict[str, str], UploadFile | None]:
    """Parse a multipart body into string fields plus its file part.

    Args:
        request: Incoming request (multipart form).

    Returns:
        Tuple of (string fields, the ``file`` part or None).
    """
    form = await request.form()
    fields = {key: value for key, value in form.items() if isinstance(value, str)}
    upload = form.get("file")
    return fields, upload if isinstance(upload, UploadFile) else None


#: ``audio_jobs.status`` labels shown by the tools job tables.
JOB_STATUS_LABELS = {
    "STARTING": "en cola",
    "GENERATING": "generando",
    # Phase 8.4: a long document render that handed the engine to a scheduled
    # digest. Shown as its own state so a deliberate wait is never mistaken for
    # a stalled job.
    "PAUSED": "en pausa (digest)",
    "FINISHED_OK": "completado",
    "FINISHED_ERROR": "error",
}
#: Badge class per run status (see .badge in base.html); statuses without an
#: explicit class fall back to themselves.
RUN_BADGES = {
    "ok": "ok",
    "completed": "ok",
    "no_new_articles": "ok",
    "failed": "failed",
    "verification_failed": "failed",
    "timed_out": "failed",
    "running": "running",
}
#: Badge class of ``audio_jobs.status`` (see .badge in base.html).
JOB_STATUS_BADGES = {
    "STARTING": "running",
    "GENERATING": "running",
    "PAUSED": "paused",
    "FINISHED_OK": "ok",
    "FINISHED_ERROR": "failed",
}

STATUS_LABELS = {
    "ok": "ok",
    "completed": "ok",
    "no_new_articles": "sin novedades",
    "failed": "error",
    "running": "en ejecución",
    "terminated": "parado",
    "canceled": "cancelado",
    "timed_out": "tiempo agotado",
    "continued_as_new": "continúa como nuevo",
    "verification_failed": "verificación fallida",
}


def _collections() -> list:
    """Return all collections from collections.toml."""
    return CollectionsRepository().list()


async def _omni_options() -> dict:
    """Fetch OmniVoice languages and voices for the TTS dropdowns.

    Returns:
        Dict with 'languages' (sorted unique codes) and 'voices' (stock +
        cloned, tagged with engine and category by the voice registry). Empty
        lists if OmniVoice is unreachable.
    """
    languages, voices = await gateway_options()
    return _registry_options(languages, voices)


def _mixed_engine_message(voice: str, voice2: str) -> str:
    """Explain a two-voice selection that mixes two engines (Phase 9.2).

    Args:
        voice: Native/single voice id from the form.
        voice2: Target voice id (dual mode only).

    Returns:
        Empty when the selection is safe (or fewer than two voices are set),
        otherwise a human-readable reason the submit button stays disabled.
    """
    first, second = (voice or "").strip(), (voice2 or "").strip()
    if not first or not second:
        return ""
    engines_first, engines_second = engines_for_voice(first), engines_for_voice(second)
    if engines_first and engines_second and not (engines_first & engines_second):
        return (
            f"Voces de motores distintos: {first} ({', '.join(sorted(engines_first))}) "
            f"y {second} ({', '.join(sorted(engines_second))}). "
            "Elige voces del mismo motor."
        )
    return ""


#: Maximum number of radio results a picker renders before the count hint
#: asks the operator to narrow the search (the edge catalogue alone has ~700).
VOICE_PICKER_LIMIT = 20


def _picker_results_context(
    entries: list,
    *,
    field: str,
    current: str = "",
    q: str = "",
    lang: str = "",
) -> dict:
    """Filter, order and cap the radio results of one voice picker (Phase 9.7).

    The glob query and the language prefix run through
    :mod:`src.api.services.voice_search`, then the category order (designs,
    clones, stock) applies and the list is capped to
    :data:`VOICE_PICKER_LIMIT`. The current selection is always kept —
    prepended when it falls past the cap or is unknown to every catalogue —
    so no re-render can drop what was picked; an unknown id keeps the exact
    text the operator typed, which the guards then treat as a wildcard.

    Args:
        entries: Registry dicts ({id, label, language, engines, category})
            or :class:`VoiceOption` objects.
        field: Radio group name ('voice' or 'voice2').
        current: Voice id currently checked in this group ('' = none).
        q: Glob-style query (see :func:`voice_search.matches`).
        lang: Language prefix filter ('es' matches es and es-ES).

    Returns:
        Context for ``partials/_voice_picker_results.html``: field, current,
        q, lang, shown (capped, tagged), total matches, limit and the
        literal option (query text to send as-is when nothing matched).
    """
    dicts = [entry if isinstance(entry, dict) else entry.model_dump() for entry in entries]
    ordered = voice_search.picker_order(voice_search.filter_voices(dicts, query=q, lang=lang))
    total = len(ordered)
    shown = ordered[:VOICE_PICKER_LIMIT]
    shown_ids = {str(entry["id"]) for entry in shown}
    if current and current not in shown_ids:
        existing = next((entry for entry in ordered if str(entry["id"]) == current), None)
        if existing is None:
            existing = {
                "id": current,
                "label": current,
                "language": "",
                "engines": [],
                "category": "",
            }
        shown = [existing, *[entry for entry in shown if str(entry["id"]) != current]]
    tagged = [
        {
            **entry,
            "tag": " · ".join(
                part
                for part in (
                    "/".join(str(engine) for engine in (entry.get("engines") or [])),
                    entry.get("category") or "",
                )
                if part
            ),
        }
        for entry in shown
    ]
    text = q.strip()
    return {
        "field": field,
        "current": current,
        "q": q,
        "lang": lang,
        "shown": tagged,
        "total": total,
        "limit": VOICE_PICKER_LIMIT,
        "literal": bool(text) and total == 0 and text != current,
    }


def _picker_lang_param(
    *,
    field: str,
    lang: str,
    language: str,
    mode: str = "",
    native_language: str = "",
    target_language: str = "",
) -> str:
    """Resolve the effective language filter of one picker (Phase 9.8).

    The document and lesson forms drive the picker language from their own
    fields instead of the old per-picker language select: the doc form sends
    ``language`` and the lesson form sends ``native_language``/
    ``target_language`` plus ``mode``. An explicit ``lang`` param (the legacy
    fragment tests) still wins; otherwise the mapping follows the lesson mode
    so the picker only offers voices that can voice that role's language —
    'dual' pins the native picker to ``native_language`` and the target picker
    to ``target_language'', while 'mixed'/'native' filter everything by the
    target language (the class is taught there).

    Args:
        field: Radio group ('voice' or 'voice2').
        lang: Legacy explicit language prefix ('' in the forms).
        language: Document form language field.
        mode: Lesson mode ('dual', 'mixed', 'native' or '' for documents).
        native_language: Lesson native language field.
        target_language: Lesson target language field.

    Returns:
        The language prefix to filter by ('' = no filter applied).
    """
    if (lang or "").strip():
        return lang.strip()
    lesson_mode = str(mode or "").strip().lower()
    if lesson_mode in {m.value for m in LessonMode}:
        if field == "voice2" and lesson_mode == "dual":
            return (target_language or "").strip()
        if lesson_mode == "dual":
            return (native_language or "").strip()
        return (target_language or "").strip()
    return (language or "").strip()


def _lesson_preview_spec(
    mode: str,
    target_language: str,
    native_language: str,
    voice: str,
    voice2: str,
) -> str:
    """Render the NarrationSpec the current lesson form would generate.

    Mirrors :func:`src.api.services.lesson.resolve_cast` for the no-spec path
    (the form preview of the cast avanzado box): the spec is what the engine
    would really voice — one role for mixed/native, two pinned to their
    languages in dual. The JSON is pretty-printed so the operator can read it
    in the preview window before pasting it into the spec textarea.

    Args:
        mode: Normalized lesson mode ('mixed', 'dual' or 'native').
        target_language: Language being taught.
        native_language: Student's native language.
        voice: Selected native/single voice id ('' when none yet).
        voice2: Selected target voice id (dual only).

    Returns:
        Pretty-printed NarrationSpec JSON for the current selection.
    """
    import json

    if mode == "dual":
        roles = [
            {
                "name": "profe_native",
                "voice_id": voice or "",
                "language": native_language or None,
            },
            {
                "name": "profe_target",
                "voice_id": voice2 or "",
                "language": target_language or None,
            },
        ]
    else:
        roles = [{"name": "teacher", "voice_id": voice or "", "language": None}]
    return json.dumps(
        {"language": target_language or "", "roles": roles},
        ensure_ascii=False,
        indent=2,
    )


def _lesson_preview_ready(mode: str, voice: str, voice2: str) -> bool:
    """Tell whether the current cast has enough voices to actually render.

    Args:
        mode: Normalized lesson mode.
        voice: Selected native/single voice id.
        voice2: Selected target voice id (dual only).

    Returns:
        True when every role the mode needs has a voice chosen.
    """
    if mode == "dual":
        return bool((voice or "").strip() and (voice2 or "").strip())
    return bool((voice or "").strip())


def _picker_context(
    entries: list,
    *,
    picker_id: str,
    field: str,
    current: str = "",
    q: str = "",
    lang: str = "",
    engines: str = "all",
    empty_label: str = "",
    guard: bool = False,
    langs: bool = True,
    lang_input: str = "",
) -> dict:
    """Build the full context of one tools voice picker (Phase 9.7).

    Wraps :func:`_picker_results_context` with everything the reusable
    ``partials/_voice_picker.html` needs: the hidden field/engine hints the
    search fragment reads, the language options of the **unfiltered**
    catalogue (stable while typing) and the guard flag that makes the
    results container re-run the lesson fragment on selection.

    Args:
        entries: Registry dicts or VoiceOption objects of this vocabulary.
        picker_id: Unique DOM id segment ('doc', 'voice', 'voice2').
        field: Radio group name ('voice' or 'voice2').
        current: Currently checked voice id for the group.
        q: Pre-filled glob query.
        lang: Pre-filled language prefix.
        engines: 'all' or the canonical engine the fragment must restrict
            itself to (the doc picker sends 'edge').
        empty_label: Label of the value="" option ('' omits it).
        guard: True for the lesson pickers (results container listens to
            radio changes and re-fetches /ui/fragments/lesson_voices).
        langs: False omits the language options dict (Phase 9.8: the forms
            already carry their own language selects, so the pickers render
            no internal language select).
        lang_input: CSS selector (or comma list) of the form's language
            field(s) the picker must include in its htmx requests so the
            debounced search keeps the language filter (Phase 9.8).

    Returns:
        Results context plus picker_id, engines, empty_label, guard, lang_input
        and langs (``[{'code', 'count'}]`` over the whole vocabulary).
    """
    dicts = [entry if isinstance(entry, dict) else entry.model_dump() for entry in entries]
    context = _picker_results_context(dicts, field=field, current=current, q=q, lang=lang)
    context.update(
        {
            "picker_id": picker_id,
            "engines": engines,
            "empty_label": empty_label,
            "guard": guard,
            "lang_input": lang_input,
            "langs": voice_search.language_options(dicts) if langs else [],
        }
    )
    return context


def _lesson_voice_context(
    voices: list[dict],
    mode: str = "mixed",
    voice: str = "",
    voice2: str = "",
    *,
    fragment: bool = False,
    voice_q: str = "",
    voice2_q: str = "",
    native_language: str = "",
    target_language: str = "",
) -> dict:
    """Build the template context of the lesson voice picker(s) (Phase 7.12).

    Shared by the tools page and its voice fragment so the initial render and
    every mode switch render exactly the same block. The catalogue is the
    full Phase 9 registry (every engine, tagged); each picker gets its radio
    results (glob search + language filter, Phase 9.7/9.8) and its guard flag
    so a click on any result re-runs this fragment and the Phase 9.2 mixed
    engine guard recomputes. The typed queries ride along as named fields
    (``voice_q``/``voice2_q``) so a re-render keeps the operator's filter. A
    current selection no source offers (gateway down, legacy id) is kept in
    the vocabulary instead of dropped, and an unknown mode falls back to
    'mixed', the single-voice default.

    Since Phase 9.8 the pickers filter by the form's own language selects:
    in 'dual' the native picker asks voices of ``native_language`` and the
    target picker voices of ``target_language``; in 'mixed'/'native' a single
    role is filtered by the target language (the class is taught there). The
    pickers therefore render no internal language select of their own.

    Args:
        voices: Raw voice entries from :func:`list_all_voices`.
        mode: Lesson mode from the form ('mixed', 'dual' or 'native').
        voice: Voice id currently selected for the native/single role.
        voice2: Voice id currently selected for the target role (dual only).
        fragment: True when rendering the htmx fragment, which additionally
            carries the out-of-band submit button (Phase 9.2 guard) and the
            live cast preview (Phase 9.8).
        voice_q: Typed glob query of the native/single picker.
        voice2_q: Typed glob query of the target picker (dual only).
        native_language: Lesson native language (drives the native picker in
            'dual'; the 'mixed'/'native' picker filters by the target).
        target_language: Lesson target language (drives the single picker and
            the 'dual' target picker).

    Returns:
        Context keys for ``partials/_lesson_voices.html``: mode, the
        ``voice_picker``/``voice2_picker`` dicts, the mixed-engine message,
        the fragment flag and the JSON ``preview_spec`` for the cast box.
    """
    normalized = str(mode or "").strip().lower()
    if normalized not in {m.value for m in LessonMode}:
        normalized = LessonMode.MIXED.value
    entries = _normalize_voice_entries(voices)
    known = {entry["id"] for entry in entries}
    current_voice = (voice or "").strip()
    current_voice2 = (voice2 or "").strip()
    for current in dict.fromkeys((current_voice, current_voice2)):
        if current and current not in known:
            entries.append(
                {"id": current, "label": current, "language": "", "engines": [], "category": ""}
            )
            known.add(current)
    dual = normalized == "dual"
    native = (native_language or "").strip()
    target = (target_language or "").strip()
    lesson_lang_input = "[name='native_language'], [name='target_language']"
    return {
        "mode": normalized,
        "lesson_voices": entries,
        "voice_picker": _picker_context(
            entries,
            picker_id="voice",
            field="voice",
            current=current_voice,
            q=voice_q,
            lang=target if not dual else native,
            empty_label="(elige una voz)" if dual else "(por defecto)",
            guard=True,
            langs=False,
            lang_input=lesson_lang_input,
        ),
        "voice2_picker": _picker_context(
            entries,
            picker_id="voice2",
            field="voice2",
            current=current_voice2,
            q=voice2_q,
            lang=target,
            empty_label="(elige una voz)",
            guard=True,
            langs=False,
            lang_input=lesson_lang_input,
        ),
        "mixed_message": _mixed_engine_message(voice, voice2),
        "fragment": fragment,
        "preview_spec": _lesson_preview_spec(
            normalized, target, native, current_voice, current_voice2
        ),
        "preview_ready": _lesson_preview_ready(normalized, current_voice, current_voice2),
    }


#: Engines offered by the per-collection TTS dropdown, in menu order. One label
#: per value of ``CollectionConfig.tts_backend``; the gateway keeps its old
#: OmniVoice name because that is what ``TTSBackend`` spells it.
TTS_BACKEND_CHOICES: tuple[tuple[str, str], ...] = (
    ("edge", "edge (local)"),
    ("omni_voice", "tts-api gateway"),
    ("kokoro", "Kokoro (gateway)"),
    ("qwen3_tts", "Qwen3-TTS (Modal)"),
    ("zonos2", "ZONOS2 (Modal)"),
    ("voxcpm2", "VoxCPM2 (Modal)"),
)


def _normalize_voice_entries(entries: list[dict]) -> list[dict]:
    """Reshape engine-specific voice dicts into one template contract.

    Args:
        entries: Raw voice dicts from an engine (gateway, catalog, Edge).

    Returns:
        List of ``{'id', 'label', 'language'}`` dicts, sorted for stable HTML.
    """
    normalized: list[dict] = []
    for item in entries:
        identifier = str(item.get("id") or item.get("voice_id") or item.get("name") or "").strip()
        if not identifier:
            continue
        normalized.append(
            {
                "id": identifier,
                "label": str(item.get("label") or item.get("name") or identifier),
                "language": str(item.get("language") or ""),
                "engines": [str(engine) for engine in (item.get("engines") or [])],
                "category": str(item.get("category") or ""),
            }
        )
    return sorted(normalized, key=lambda v: (v["language"], v["label"]))


def _registry_options(languages: list[str], voices: list) -> dict:
    """Reshape a registry source into the dropdown context contract.

    Args:
        languages: Language codes the source reported.
        voices: Registry :class:`VoiceOption` objects from one source.

    Returns:
        Dict with 'languages' and normalized 'voices' (id, label, language,
        engines, category).
    """
    return {
        "languages": languages,
        "voices": _normalize_voice_entries([v.model_dump() for v in voices]),
    }


async def _edge_voice_options() -> dict:
    """Fetch the real edge-tts catalogue (ShortNames) for the dropdown.

    This is the default engine, so the operator must be able to pick an actual
    Edge voice instead of typing one from memory. The catalogue is cached at
    module level by ``src.api.services.tts``, so the cost is one call per
    process; a failure degrades to a free-text input rather than breaking the
    page.

    Returns:
        Dict with 'languages' (ISO 639-1 codes) and normalized 'voices'.
    """
    languages, voices = await edge_options()
    return _registry_options(languages, voices)


def _modal_voice_options() -> dict:
    """Fetch the Phase 7.9 voice catalog for the Modal dropdowns.

    Returns:
        Dict with 'languages' and normalized 'voices', empty when the catalog
        file is missing (a Modal collection then falls back to a text input).
    """
    languages, voices = modal_options()
    return _registry_options(languages, voices)


async def _tts_voice_options(backend: str) -> dict:
    """Return the language and voice catalogue of one TTS engine.

    Each engine has its own voice vocabulary (Edge ShortNames, tts-api stock ids
    or clone UUIDs, Modal catalog names), so the form must offer the ids that
    engine actually accepts: showing an Edge voice while the gateway is selected
    would only fail at 08:00 during the run.

    Args:
        backend: Canonical engine id ('edge', 'omni_voice', 'qwen3_tts', ...).

    Returns:
        Dict with 'languages' and normalized 'voices'; both empty when the
        catalogue is unavailable (the partial then renders text inputs).
    """
    if backend == "edge":
        return await _edge_voice_options()
    if backend in ("qwen3_tts", "zonos2", "voxcpm2"):
        return _modal_voice_options()
    gateway = await _omni_options()
    return {
        "languages": gateway["languages"],
        "voices": _normalize_voice_entries(gateway["voices"]),
    }


async def _tts_options_by_backend(backends: set[str]) -> dict[str, dict]:
    """Fetch the voice catalogue of every engine the page has to render.

    Args:
        backends: Canonical engine ids in use by the collections on the page.

    Returns:
        Mapping of engine id to its catalogue.
    """
    return {backend: await _tts_voice_options(backend) for backend in sorted(backends)}


def _split_list(raw: str) -> list[str]:
    """Split a textarea value into a cleaned, de-duplicated list.

    Accepts newline- or comma-separated values; blank entries are dropped and
    the original order is preserved. Format validation (E.164, email shape) is
    left to the Pydantic models.

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed values.
    """
    seen: list[str] = []
    for item in raw.replace(",", "\n").splitlines():
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _split_emails(raw: str) -> list[str]:
    """Split a form value into a cleaned list of email recipients.

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed addresses.
    """
    return _split_list(raw)


def _split_whatsapp(raw: str) -> list[str]:
    """Split a form value into a cleaned list of WhatsApp numbers (E.164).

    Args:
        raw: Raw textarea value.

    Returns:
        List of unique, non-empty trimmed numbers. Format validation happens in
        `CollectionConfig`.
    """
    return _split_list(raw)


def _parse_key_value(raw: str) -> dict[str, str]:
    """Parse a textarea of ``key=value`` lines into a dict.

    Args:
        raw: Raw textarea value with one ``key=value`` pair per line.

    Returns:
        Dict of trimmed keys to trimmed values. Blank lines and lines without
        ``=`` are ignored.
    """
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _parse_reliability(raw: str) -> dict[str, float]:
    """Parse a textarea of ``url=0.9`` lines into a reliability map.

    Args:
        raw: Raw textarea value with one ``url=score`` pair per line.

    Returns:
        Dict of feed URLs to reliability scores. Invalid scores are skipped.
    """
    result: dict[str, float] = {}
    for key, value in _parse_key_value(raw).items():
        try:
            result[key] = float(value)
        except ValueError:
            continue
    return result


def _parse_hosts(raw: str) -> dict[str, list[str]]:
    """Parse a textarea of ``url=host1,host2`` lines into a host map.

    Args:
        raw: Raw textarea value with one ``url=hosts`` pair per line.

    Returns:
        Dict of feed URLs to lists of trimmed, lowercase hosts.
    """
    result: dict[str, list[str]] = {}
    for key, value in _parse_key_value(raw).items():
        hosts = [h.strip().lower() for h in value.split(",") if h.strip()]
        if hosts:
            result[key] = hosts
    return result


DELIVERY_CHANNELS = ("telegram", "email", "whatsapp")
LOGIC_CHANNELS = ("telegram", "email", "whatsapp")
LOGIC_FORM_KEYS = (
    "logic_chat_id",
    "logic_email_recipients",
    "logic_whatsapp_recipients",
    "logic_whatsapp_group_id",
)


async def _checked_values(request: Request, field: str, allowed: tuple[str, ...]) -> list[str]:
    """Extract the selected checkboxes of a form body.

    Args:
        request: Incoming form request.
        field: Checkbox input name.
        allowed: Accepted values, in canonical order.

    Returns:
        Ordered, de-duplicated list of checked values.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    values = [
        value
        for key, value in parse_qsl(raw, keep_blank_values=True)
        if key == field and value in allowed
    ]
    return list(dict.fromkeys(values))


async def _selected_channels(request: Request) -> list[str]:
    """Extract the delivery channel checkboxes from a form body.

    Values are 'telegram', 'email' and 'whatsapp'; an empty selection (no
    checkbox ticked) falls back to the default telegram-only delivery.

    Args:
        request: Incoming form request.

    Returns:
        Ordered, validated list of selected delivery channels.
    """
    return await _checked_values(request, "delivery_channels", DELIVERY_CHANNELS) or ["telegram"]


def _logic_delivery_from_form(
    fields: dict[str, str],
    channels: list[str],
    current: LogicDeliveryConfig | None,
) -> LogicDeliveryConfig | None:
    """Build the optional `logic_delivery` block from form fields.

    When the block is absent from the body (legacy or programmatic clients) the
    current value is preserved, so an update never erases the configuration. When
    the block is present but completely empty the destination is disabled.

    Args:
        fields: Parsed form fields.
        channels: Checked logic delivery channels.
        current: Existing configuration to preserve when the block is absent.

    Returns:
        The logic delivery configuration, or None when disabled.
    """
    if not channels and not any(key in fields for key in LOGIC_FORM_KEYS):
        return current
    chat_id = fields.get("logic_chat_id", "").strip() or None
    email_recipients = _split_emails(fields.get("logic_email_recipients", ""))
    whatsapp_recipients = _split_whatsapp(fields.get("logic_whatsapp_recipients", ""))
    group_id = fields.get("logic_whatsapp_group_id", "").strip() or None
    if not (channels or chat_id or email_recipients or whatsapp_recipients or group_id):
        return None
    return LogicDeliveryConfig(
        channels=channels,
        chat_id=chat_id,
        email_recipients=email_recipients,
        whatsapp_recipients=whatsapp_recipients,
        whatsapp_group_id=group_id,
    )


async def start_collection_podcast(name: str) -> tuple[str, str]:
    """Resolve and start the podcast render of a collection's latest digest.

    Shared by the tools page and the collections page buttons (Phase 7.12 UI):
    the script is generated by the LLM from that run's digest, so the render
    only starts once the collection has at least one run.

    Args:
        name: Collection name (as typed/submitted).

    Returns:
        Tuple of (flash level, message): 'ok' with both ids, or 'error' with
        the reason nothing started.
    """
    collection = CollectionsRepository().resolve(name)
    if collection is None:
        return "error", f"Colección '{name}' no encontrada"
    if not collection.narration_spec:
        return (
            "error",
            f"La colección '{name}' no tiene NarrationSpec (véase Colecciones)",
        )
    runs = await RunsRepository().list_runs(collection=collection.name, limit=20)
    # Prefer a real digest run; URL summaries also land in digest_runs but are
    # not what the podcast prompt expects as its source story.
    source = next(
        (r for r in runs if str(r.get("workflow_id", "")).startswith("digest-pipeline-")),
        runs[0] if runs else None,
    )
    if source is None:
        return "error", f"'{name}' aún no tiene ejecuciones; genera un digesto primero"
    run_id = str(source["workflow_id"])
    try:
        result = await TemporalAdminService().start_narration_render(
            collection=collection.name,
            spec=collection.narration_spec,
            strategy=collection.narration_strategy,
            run_id=run_id,
        )
    except SubmitError as exc:
        return "error", exc.message
    except Exception as exc:
        logger.error("ui_podcast_failed", collection=name, error=str(exc), exc_info=True)
        return "error", f"No se pudo lanzar: {exc}"
    return "ok", f"Podcast lanzado desde {run_id}: {result['workflow_id']}"


def _flash(redirect_url: str, message: str, level: str = "ok") -> RedirectResponse:
    """Redirect with a flash message attached as a query parameter.

    Args:
        redirect_url: Target page URL.
        message: Message to show to the user.
        level: Flash level ('ok', 'warn' or 'error').

    Returns:
        A RedirectResponse carrying the flash in its query string.
    """
    import urllib.parse

    sep = "&" if "?" in redirect_url else "?"
    redirect = f"{redirect_url}{sep}flash={urllib.parse.quote(message)}&level={level}"
    return RedirectResponse(redirect, 302)


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    """Render a template with the base context.

    Args:
        request: Incoming request (for Jinja2 URLs).
        name: Template file name.
        context: Page-specific context dict.

    Returns:
        An HTMLResponse.
    """
    context.setdefault("request", request)
    context.setdefault("flash", request.query_params.get("flash", ""))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    context.setdefault("active", name.split(".")[0])
    # Phase 6: the middleware injects the resolved identity; public pages
    # (login/register) fall back to an empty role so the nav stays hidden.
    role = getattr(request.state, "role", None)
    context.setdefault("role", role)
    context.setdefault("current_user", getattr(request.state, "user", ""))
    context.setdefault("is_admin", role is not None and str(role) == "admin")
    context.setdefault("can_operate", str(role) in ("admin", "user"))
    return templates.TemplateResponse(request, name, context)


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse an HH:MM collection time into (hour, minute).

    Args:
        time_str: Time in HH:MM format.

    Returns:
        (hour, minute) tuple; falls back to (8, 0) on bad input.
    """
    try:
        hour, minute = (int(part) for part in time_str.split(":"))
        return hour, minute
    except (TypeError, ValueError):
        return 8, 0


def _week_grid(collections: list) -> list[dict]:
    """Build a 7-day weekly grid marking collection schedule times.

    Args:
        collections: List of collection configs.

    Returns:
        List of dicts keyed by weekday index with the scheduled collections.
    """
    grid: list[dict] = [{"name": name, "slots": []} for name in WEEKDAY_NAMES]
    for collection in collections:
        if not collection.enabled:
            continue
        hour, minute = _parse_time(collection.time)
        for day in range(7):
            grid[day]["slots"].append({"name": collection.name, "hour": hour, "minute": minute})
    for day in grid:
        day["slots"].sort(key=lambda s: (s["hour"], s["minute"]))
    return grid


@router.get("/ui", include_in_schema=False)
async def ui_home() -> RedirectResponse:
    """Redirect the /ui root to the collections page."""
    return RedirectResponse("/ui/collections")


@router.get("/ui/collections", response_class=HTMLResponse, include_in_schema=False)
async def ui_collections(request: Request) -> HTMLResponse:
    """Render the collections management page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the collections page.
    """
    collections = _collections()
    # The cast editor needs the voice catalog even when TTS is off for every
    # collection (a NarrationSpec can exist without digest TTS).
    omni = (
        await _omni_options()
        if any(c.tts_enabled or c.narration_spec for c in collections)
        else {"languages": [], "voices": []}
    )
    # Phase 8: the engine selects the voice vocabulary, so the page fetches the
    # catalogue of every engine its collections actually use (at most four) —
    # and only when some collection would synthesize at all.
    from src.api.services.tts_backends import resolve_backend

    tts_options = (
        await _tts_options_by_backend(
            {resolve_backend(c.tts_backend or None).value for c in collections} or {"edge"}
        )
        if any(c.tts_enabled for c in collections)
        else {}
    )
    # Phase 9.2: the cast datalists follow the narration engine of each form,
    # so the page also fetches the catalogue of every engine in narration use.
    cast_engines = {
        str(c.narration_spec.tts_backend) if c.narration_spec else "omni_voice" for c in collections
    }
    cast_engines.add("omni_voice")  # the create form defaults to the gateway
    cast_options = await _tts_options_by_backend(cast_engines)
    return _render(
        request,
        "collections.html",
        {
            "collections": collections,
            "omni": omni,
            "tts_backends": TTS_BACKEND_CHOICES,
            "tts_options": tts_options,
            "cast_options": cast_options,
            "emotion_tags": EMOTION_TAGS,
        },
    )


@router.get("/ui/calendar", response_class=HTMLResponse, include_in_schema=False)
async def ui_calendar(request: Request) -> HTMLResponse:
    """Render the weekly schedule calendar page.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the calendar page.
    """
    collections = _collections()
    today = datetime.now(UTC).date()
    week_start = today - timedelta(days=today.weekday())
    return _render(
        request,
        "calendar.html",
        {
            "collections": _week_grid(collections),
            "week_start": week_start,
            "today": today,
            "hour_range": range(24),
            "weekday_names": WEEKDAY_NAMES,
        },
    )


def _collection_from_workflow_id(workflow_id: str, known: list[str]) -> str:
    """Best-effort parse of the collection name from a digest workflow id.

    Matches the longest known collection name that prefixes the id suffix;
    falls back to stripping the trailing timestamp.

    Args:
        workflow_id: Temporal workflow id ('digest-pipeline-<name>-<ts>').
        known: Known collection names.

    Returns:
        The inferred collection name.
    """
    suffix = workflow_id.removeprefix("digest-pipeline-")
    for name in sorted(known, key=len, reverse=True):
        if suffix == name or suffix.startswith(f"{name}-"):
            return name
    return suffix.rsplit("-", 1)[0]


def _fmt_utc(value: datetime | None) -> str:
    """Format a UTC datetime for display.

    Args:
        value: Datetime or None.

    Returns:
        'YYYY-MM-DD HH:MM UTC' or an empty string when None.
    """
    if value is None:
        return ""
    return f"{value.strftime('%Y-%m-%d %H:%M')} UTC"


def _fmt_iso(value: str | None) -> str:
    """Format an ISO timestamp string (as stored in the DB) for display.

    Args:
        value: ISO string like '2026-09-14T08:00:00+00:00' or None.

    Returns:
        'YYYY-MM-DD HH:MM UTC' or an empty string when None.
    """
    if not value:
        return ""
    return f"{value[:16].replace('T', ' ')} UTC"


async def _executions(limit: int = 50) -> list[dict]:
    """Merge Temporal visibility executions with the DB run history.

    Inventory of the digest pipeline's executions: Temporal visibility provides
    the real status (running/completed/failed/terminated/...) while the DB adds
    article/cluster counts, digest preview and stored error messages.

    Args:
        limit: Maximum number of Temporal executions to fetch.

    Returns:
        Merged list of run dicts, newest first.
    """
    service = TemporalAdminService()
    known = [c.name for c in _collections()]
    try:
        execs = await service.list_executions(limit=limit)
    except Exception as exc:
        logger.warning("list_executions_failed", error=str(exc))
        execs = []

    db_runs = await RunsRepository().list_runs(limit=300)
    db_by_wf = {r["workflow_id"]: r for r in db_runs}

    merged: list[dict] = []
    seen: set[str] = set()
    for exec in execs:
        wf = exec["workflow_id"]
        seen.add(wf)
        db = db_by_wf.get(wf, {})
        merged.append(
            {
                "workflow_id": wf,
                "collection": db.get("collection") or _collection_from_workflow_id(wf, known),
                "status": exec["status"],
                "start_label": _fmt_utc(exec["start_time"]),
                "end_label": _fmt_utc(exec["close_time"]),
                "article_count": db.get("article_count"),
                "cluster_count": db.get("cluster_count"),
                "digest_preview": db.get("digest_preview"),
                "error": db.get("error"),
                "run_id": db.get("id"),
            }
        )
    for wf, db in db_by_wf.items():
        if wf in seen:
            continue
        merged.append(
            {
                "workflow_id": wf,
                "collection": db.get("collection"),
                "status": db["status"],
                "start_label": _fmt_iso(db.get("started_at")),
                "end_label": _fmt_iso(db.get("completed_at")),
                "article_count": db.get("article_count"),
                "cluster_count": db.get("cluster_count"),
                "digest_preview": db.get("digest_preview"),
                "error": db.get("error"),
                "run_id": db.get("id"),
            }
        )
    merged.sort(key=lambda r: r["start_label"], reverse=True)
    return merged


@router.get("/ui/runs", response_class=HTMLResponse, include_in_schema=False)
async def ui_runs(request: Request) -> HTMLResponse:
    """Render the executions page (merged Temporal + DB runs).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the executions page.
    """
    return _render(request, "runs.html", {})


#: Filter options of the "Todas las voces" table (Phase 9.6): value and label
#: for the Tipo and Motor selects; the Idioma options come from the registry.
CATEGORY_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "Todos"),
    ("stock", "stock"),
    ("cloned", "clonada"),
    ("generated", "diseñada"),
)
ENGINE_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "Todos"),
    ("edge", "edge"),
    ("omni_voice", "tts-api gateway"),
    ("kokoro", "kokoro"),
    ("qwen3_tts", "qwen3_tts"),
    ("zonos2", "zonos2"),
    ("voxcpm2", "voxcpm2"),
)


async def _voices_table_context(
    voices: list | None = None,
    *,
    q: str = "",
    lang: str = "",
    category: str = "",
    engine: str = "",
    page: str | int = 1,
    size: str | int = voice_search.DEFAULT_PAGE_SIZE,
) -> dict:
    """Build the context of the unified voice table (Phase 9.6).

    One pass over the registry applies the glob query and the structured
    filters, then paginates; the design status of every Modal profile is
    joined in so a row can badge "pendiente/generada/desactualizada" and
    link its reference WAV. A unreadable manifest degrades to no status
    instead of breaking the listing.

    Args:
        voices: Optional pre-fetched registry list (the page needs it once
            more for the language options).
        q: Glob-style query over id, label, language, category and engines.
        lang: Language prefix filter ('es' matches es and es-ES).
        category: Exact category filter ('stock', 'cloned', 'generated').
        engine: Canonical engine id the voice must accept.
        page: 1-based page number (str from the query string is fine).
        size: Page size.

    Returns:
        Context keys for ``partials/_voices_table.html``: rows, page_obj
        and the urlencoded filter state (qs) the pager links ride on.
    """
    from src.core.voice_catalog import VoiceCatalogError

    if voices is None:
        voices = await list_all_voices()
    filtered = voice_search.filter_voices(
        voices, query=q, lang=lang, category=category, engine=engine
    )
    page_obj = voice_search.paginate(filtered, page=page, size=size)
    status_by_name: dict[str, dict] = {}
    try:
        for row in await voice_catalog_admin.generated_voices():
            status_by_name[row["profile"].name] = row
    except VoiceCatalogError as exc:
        logger.warning("voices_table_status_failed", error=str(exc))
    rows: list[dict] = []
    for voice in page_obj.items:
        status_row = status_by_name.get(voice.id)
        status = str(status_row["status"]) if status_row else ""
        rows.append(
            {
                "id": voice.id,
                "label": voice.label,
                "language": voice.language,
                "category": voice.category,
                "engines": voice.engines,
                "status": status,
                "badge": str(status_row["badge"]) if status_row else "",
                "has_audio": status == voice_catalog_admin.STATUS_GENERATED,
            }
        )
    filters = {
        key: value
        for key, value in {
            "q": q,
            "lang": lang,
            "category": category,
            "engine": engine,
            "size": page_obj.size if page_obj.size != voice_search.DEFAULT_PAGE_SIZE else "",
        }.items()
        if value
    }
    return {
        "rows": rows,
        "page_obj": page_obj,
        "qs": urlencode(filters),
    }


@router.get("/ui/voices", response_class=HTMLResponse, include_in_schema=False)
async def ui_voices(request: Request) -> HTMLResponse:
    """Render the voices management page.

    The listing is the unified Phase 9.6 table (clones, stock and designs
    with filter row and pagination) rendered inline on first paint; the
    generated-voices card keeps its 15 s poll while designs run, and the
    custom/clone forms are unchanged.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the voices page.
    """
    voices = await list_all_voices()
    table = await _voices_table_context(voices)
    return _render(
        request,
        "voices.html",
        {
            **table,
            "q": "",
            "lang": "",
            "category": "",
            "engine": "",
            "size": voice_search.DEFAULT_PAGE_SIZE,
            "lang_options": voice_search.language_options(voices),
            "category_filters": CATEGORY_FILTERS,
            "engine_filters": ENGINE_FILTERS,
        },
    )


@router.get("/ui/fragments/voices_table", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_voices_table(
    request: Request,
    q: str = "",
    lang: str = "",
    category: str = "",
    engine: str = "",
    page: str = "1",
    size: str = "25",
) -> HTMLResponse:
    """Render one page of the unified voice table (Phase 9.6).

    Swapped into ``#voices-table`` by the filter row (keyup with 300 ms
    debounce on the query, change on the selects) and by the pager links.
    The request never carries ``page`` from the filter inputs, so any
    filter change restarts at page 1 while the pager links ship the full
    filter state plus their own page number.

    Args:
        request: Incoming request.
        q: Glob-style query.
        lang: Language prefix filter.
        category: Category filter ('stock', 'cloned', 'generated').
        engine: Engine filter.
        page: 1-based page number.
        size: Page size.

    Returns:
        HTMLResponse with the table rows and the pager fragment.
    """
    context = await _voices_table_context(
        q=q, lang=lang, category=category, engine=engine, page=page, size=size
    )
    return _render(request, "partials/_voices_table.html", context)


@router.get("/ui/fragments/runs", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_runs(request: Request) -> HTMLResponse:
    """Render the executions table fragment for HTMX polling.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the executions table body.
    """
    runs = await _executions()
    return _render(
        request,
        "partials/_executions.html",
        {"runs": runs, "status_labels": STATUS_LABELS},
    )


@router.get("/ui/fragments/run_detail", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_run_detail(
    request: Request, workflow_id: str, run_id: int | None = None
) -> HTMLResponse:
    """Render the detail panel for a single execution.

    Args:
        request: Incoming request.
        workflow_id: Temporal workflow id.
        run_id: Optional DB row id.

    Returns:
        HTMLResponse with the run detail partial.
    """
    detail: dict = {
        "error": None,
        "digest_preview": None,
        "article_count": None,
        "cluster_count": None,
        "narration_path": None,
        "narration_job": None,
    }
    if run_id:
        row = await RunsRepository().get_run(run_id)
        if row:
            detail = {**detail, **row}
            if row.get("narration_path"):
                detail["narration_path"] = row["narration_path"]
            # Fetch narration audio job
            job = await AudioJobsRepository().get_job(workflow_id, kind="narration")
            if job:
                detail["narration_job"] = job
    if not detail.get("error"):
        try:
            detail["error"] = await TemporalAdminService().execution_error(workflow_id)
        except Exception as exc:
            logger.warning("execution_error_failed", workflow_id=workflow_id, error=str(exc))
    # Phase 7.13/7.14: every verdict (figures, faithfulness, ASR/WER) recorded
    # for this run, so a withheld audio or a blocked digest is auditable here.
    detail["verifications"] = await VerificationsRepository().get_for_run(workflow_id)
    return _render(request, "partials/_run_detail.html", {"workflow_id": workflow_id, **detail})


@router.post("/ui/runs/{workflow_id}/terminate", include_in_schema=False)
async def ui_terminate_run(workflow_id: str) -> RedirectResponse:
    """Terminate a running execution from the GUI.

    Args:
        workflow_id: Temporal workflow id to terminate.

    Returns:
        Redirect to the executions page with a flash.
    """
    try:
        await TemporalAdminService().terminate_workflow(workflow_id)
    except Exception as exc:
        logger.error("ui_terminate_failed", workflow_id=workflow_id, error=str(exc), exc_info=True)
        return _flash("/ui/runs", f"Fallo al detener: {exc}", "error")
    return _flash("/ui/runs", f"Ejecución '{workflow_id}' detenida")


@router.get("/ui/fragments/tts_options", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_tts_options(
    request: Request,
    tts_enabled: str = "",
    tts_backend: str = "",
    language: str = "",
    voice: str = "",
) -> HTMLResponse:
    """Render the engine/language/voice fields for a collection form (Phase 8).

    The partial is re-fetched both when the TTS checkbox toggles and when the
    engine select changes, because each engine has its own voice vocabulary:
    the list must follow the engine, not stay pinned to whatever was rendered
    first. When ``tts_enabled`` is falsy the catalogues are not fetched at all.

    Args:
        request: Incoming request.
        tts_enabled: 'on' when TTS is enabled for the collection.
        tts_backend: Current engine ('edge', 'omni_voice', ...).
        language: Current language value to keep selected.
        voice: Current voice value to keep selected.

    Returns:
        HTMLResponse with the _tts_options partial.
    """
    from src.api.services.tts_backends import resolve_backend

    try:
        engine = str(resolve_backend(tts_backend or None).value)
    except Exception:  # noqa: BLE001 - an unknown engine renders as the default
        engine = "edge"
    options = await _tts_voice_options(engine) if tts_enabled else {"languages": [], "voices": []}
    return _render(
        request,
        "partials/_tts_options.html",
        {
            "tts_enabled": bool(tts_enabled),
            "tts_backends": TTS_BACKEND_CHOICES,
            "current_backend": engine,
            "tts_options": {engine: options},
            "current_language": language,
            "current_voice": voice,
        },
    )


@router.get("/ui/fragments/cast_voices", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_cast_voices(
    request: Request,
    narr_backend: str = "",
    cast: str = "",
    rows: int = 2,
) -> HTMLResponse:
    """Out-of-band options of every cast voice select for one engine (Phase 9.5).

    The cast editor's engine select re-fetches this fragment so each
    ``<select>`` only offers ids the selected engine accepts; a Modal name in
    a gateway cast (or the reverse) is what the multi-engine guard would
    reject anyway. The response carries one ``hx-swap-oob`` block per cast row
    (ids ``cast-voice-<cast>-<i>``), so a single fetch refreshes every row and
    each select resets to the empty option: the old voice belonged to the
    previous engine.

    Args:
        request: Incoming request.
        narr_backend: Engine chosen in the form ('omni_voice', 'qwen3_tts', ...).
        cast: Form identifier of the cast ('nueva' or the collection name).
        rows: Number of rows of the cast table (clamped to 1..8).

    Returns:
        HTMLResponse with the out-of-band ``<option>`` payloads.
    """
    from src.api.services.tts_backends import ALLOWED_BACKENDS

    engine = (narr_backend or "").strip().lower()
    if engine not in ALLOWED_BACKENDS:
        engine = TTSBackend.OMNIVOICE.value
    options = await _tts_voice_options(engine)
    return _render(
        request,
        "partials/_cast_voices_oob.html",
        {
            "cast": (cast or "").strip(),
            "rows": max(1, min(rows or 2, 8)),
            "voices": options["voices"],
        },
    )


@router.get("/ui/fragments/calendar", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_calendar(request: Request) -> HTMLResponse:
    """Render the calendar body fragment.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the calendar grid body.
    """
    today = datetime.now(UTC).date()
    week_start = today - timedelta(days=today.weekday())
    return _render(
        request,
        "partials/_calendar.html",
        {
            "collections": _week_grid(_collections()),
            "week_start": week_start,
            "today": today,
            "hour_range": range(24),
            "weekday_names": WEEKDAY_NAMES,
        },
    )


def _narration_spec_from_form(
    fields: dict[str, str], base: NarrationSpec | None = None
) -> NarrationSpec | None:
    """Build the NarrationSpec from the structured cast rows (GUI editor).

    The form sends one row per role (``role<i>_name``, ``role<i>_voice``,
    ``role<i>_lang``, ``role<i>_speed``, ``role<i>_emotion``) plus the spec-level
    fields (``narr_title``, ``narr_language``, ``narr_pause_ms``, ``narr_speed``,
    ``narr_emotion``, ``narr_backend``, ``narr_disclosure``). Fields the editor
    does not expose (music bed, ``max_chars_per_request``, ...) are preserved
    from ``base`` so saving the form never silently drops them.

    With no structured role filled the legacy JSON textarea wins, so old
    collections keep working unchanged.

    Args:
        fields: Form field dict.
        base: The spec being edited (None when creating a collection).

    Returns:
        The parsed NarrationSpec, or None when the collection has no cast.

    Raises:
        ValueError: On a half-filled role row or unparsable numbers, so the
            save is rejected instead of storing a broken spec.
    """
    roles: list[dict] = []
    for index in range(8):  # NarrationSpec allows at most 8 roles
        name = (fields.get(f"role{index}_name") or "").strip()
        voice = (fields.get(f"role{index}_voice") or "").strip()
        if not name and not voice:
            continue
        if not name or not voice:
            raise ValueError(f"Rol {index + 1}: el nombre y la voz van juntos")
        language = (fields.get(f"role{index}_lang") or "").strip() or None
        raw_speed = (fields.get(f"role{index}_speed") or "").strip()
        raw_pitch = (fields.get(f"role{index}_pitch") or "").strip()
        role_dict: dict = {
            "name": name,
            "voice_id": voice,
            "language": language,
            "speed": float(raw_speed) if raw_speed else 1.0,
            "emotion": (fields.get(f"role{index}_emotion") or "").strip() or None,
        }
        if raw_pitch:
            try:
                p = float(raw_pitch)
                if p < -6.0:
                    p = -6.0
                if p > 6.0:
                    p = 6.0
                role_dict["pitch_semitones"] = p
            except ValueError:
                pass
        if fields.get(f"role{index}_formant") == "off":
            role_dict["formant_preserve"] = False
        else:
            role_dict["formant_preserve"] = True
        roles.append(role_dict)

    if not roles:
        # Legacy path: the JSON textarea is the source of truth.
        raw = fields.get("narration_spec", "").strip()
        if not raw:
            return None
        try:
            return NarrationSpec.model_validate_json(raw)
        except ValidationError as exc:
            # Malformed JSON keeps the legacy lenient behaviour (ignore it),
            # but a well-formed spec that fails validation — e.g. a cast that
            # mixes engines (Phase 9.2) — must surface as an error instead of
            # silently dropping the operator's narration config.
            if any(err.get("type") == "json_invalid" for err in exc.errors()):
                return None
            raise
        except Exception:
            return None

    payload: dict = dict(base.model_dump(mode="json")) if base else {}
    raw_pause = (fields.get("narr_pause_ms") or "").strip()
    raw_speed = (fields.get("narr_speed") or "").strip()
    payload.update(
        {
            "title": (fields.get("narr_title") or "").strip() or payload.get("title", ""),
            "language": (fields.get("narr_language") or "").strip()
            or payload.get("language", "es"),
            "roles": roles,
            "pause_ms": int(raw_pause) if raw_pause else 400,
            "speed": float(raw_speed) if raw_speed else 1.0,
            "emotion": (fields.get("narr_emotion") or "").strip() or None,
            "tts_backend": (fields.get("narr_backend") or "").strip()
            or payload.get("tts_backend", "omni_voice"),
            "ai_disclosure": fields.get("narr_disclosure") == "on",
            "run_in_modal": fields.get("narr_run_in_modal") == "on",
        }
    )
    return NarrationSpec.model_validate(payload)


def _validation_message(exc: ValidationError) -> str:
    """Extract the first readable message out of a Pydantic error.

    Args:
        exc: The validation error raised while building a model from the form.

    Returns:
        'field: message' (with the pydantic 'Value error, ' prefix stripped),
        falling back to the raw error string when there is no field error.
    """
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", exc)).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


@router.post("/ui/collections", include_in_schema=False)
async def ui_create_collection(request: Request) -> RedirectResponse:
    """Create a collection from the GUI form.

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the collections page with a flash.
    """
    from src.api.models.admin import CollectionIn

    fields = await _form(request)
    name = fields.get("name", "").strip()
    if not name:
        return _flash("/ui/collections", "Nombre de colección requerido", "error")
    urls = [
        u.strip() for u in fields.get("feed_urls", "").replace(",", "\n").splitlines() if u.strip()
    ]
    try:
        config = CollectionIn(
            name=name,
            time=fields.get("time", "08:00"),
            feed_urls=urls,
            tts_enabled=fields.get("tts_enabled") == "on",
            deliver_audio=fields.get("deliver_audio") == "on",
            podcast_name=fields.get("podcast_name", ""),
            podcast_behavior=fields.get("podcast_behavior", ""),
            language=fields.get("language", ""),
            voice=fields.get("voice", ""),
            tts_backend=fields.get("tts_backend") or "edge",
            prompt_template=fields.get("prompt_template") or "default",
            custom_prompt=fields.get("custom_prompt", ""),
            chat_id=fields.get("chat_id") or None,
            email_recipients=_split_emails(fields.get("email_recipients", "")),
            whatsapp_recipients=_split_whatsapp(fields.get("whatsapp_recipients", "")),
            whatsapp_group_id=fields.get("whatsapp_group_id", "").strip() or None,
            delivery_channels=await _selected_channels(request),
            logic_delivery=_logic_delivery_from_form(
                fields, await _checked_values(request, "logic_channels", LOGIC_CHANNELS), None
            ),
            narration_enabled=fields.get("narration_enabled") == "on",
            narration_spec=_narration_spec_from_form(fields),
            narration_strategy=fields.get("narration_strategy", "turns"),
            narration_deliver_audio=fields.get("narration_deliver_audio") != "off",
            main_story_count=int(fields.get("main_story_count") or 5),
            headline_count=int(fields.get("headline_count") or 5),
            source_reliability=_parse_reliability(fields.get("source_reliability", "")),
            source_hosts=_parse_hosts(fields.get("source_hosts", "")),
            llm_verify=fields.get("llm_verify") == "on",
            llm_verify_threshold=(
                float(fields["llm_verify_threshold"])
                if fields.get("llm_verify_threshold", "").strip()
                else None
            ),
            llm_verify_backend=fields.get("llm_verify_backend", "").strip() or None,
            audio_verify=fields.get("audio_verify") == "on",
            audio_verify_threshold=(
                float(fields["audio_verify_threshold"])
                if fields.get("audio_verify_threshold", "").strip()
                else None
            ),
        ).to_config()
        CollectionsRepository().create(config)
    except CollectionConflictError as exc:
        return _flash("/ui/collections", str(exc), "error")
    except ValidationError as exc:
        return _flash("/ui/collections", f"Error al crear: {_validation_message(exc)}", "error")
    except Exception as exc:
        return _flash("/ui/collections", f"Error al crear: {exc}", "error")
    return _flash("/ui/collections", f"Colección '{name}' creada")


@router.post("/ui/collections/{name}/update", include_in_schema=False)
async def ui_update_collection(name: str, request: Request) -> RedirectResponse:
    """Update a collection from the GUI form.

    Args:
        name: Current collection name.
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the collections page with a flash.
    """
    from src.api.models.admin import CollectionIn

    fields = await _form(request)
    new_name = fields.get("new_name", name).strip()
    repo = CollectionsRepository()
    current = repo.get(name)
    if current is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    urls = [
        u.strip() for u in fields.get("feed_urls", "").replace(",", "\n").splitlines() if u.strip()
    ]
    try:
        config = CollectionIn(
            name=new_name,
            time=fields.get("time", "08:00"),
            feed_urls=urls,
            tts_enabled=fields.get("tts_enabled") == "on",
            deliver_audio=fields.get("deliver_audio") == "on",
            podcast_name=fields.get("podcast_name", ""),
            podcast_behavior=fields.get("podcast_behavior", ""),
            language=fields.get("language", ""),
            voice=fields.get("voice", ""),
            tts_backend=fields.get("tts_backend") or current.tts_backend,
            prompt_template=fields.get("prompt_template") or "default",
            custom_prompt=fields.get("custom_prompt", ""),
            chat_id=fields.get("chat_id") or None,
            email_recipients=_split_emails(fields.get("email_recipients", "")),
            whatsapp_recipients=_split_whatsapp(fields.get("whatsapp_recipients", "")),
            whatsapp_group_id=fields.get("whatsapp_group_id", "").strip() or None,
            delivery_channels=await _selected_channels(request),
            logic_delivery=_logic_delivery_from_form(
                fields,
                await _checked_values(request, "logic_channels", LOGIC_CHANNELS),
                current.logic_delivery,
            ),
            narration_enabled=fields.get("narration_enabled") == "on",
            narration_spec=_narration_spec_from_form(fields, current.narration_spec),
            narration_strategy=fields.get("narration_strategy", "turns"),
            narration_deliver_audio=fields.get("narration_deliver_audio") != "off",
            main_story_count=int(fields.get("main_story_count") or 5),
            headline_count=int(fields.get("headline_count") or 5),
            source_reliability=_parse_reliability(fields.get("source_reliability", "")),
            source_hosts=_parse_hosts(fields.get("source_hosts", "")),
            llm_verify=fields.get("llm_verify") == "on",
            llm_verify_threshold=(
                float(fields["llm_verify_threshold"])
                if fields.get("llm_verify_threshold", "").strip()
                else None
            ),
            llm_verify_backend=fields.get("llm_verify_backend", "").strip() or None,
            audio_verify=fields.get("audio_verify") == "on",
            audio_verify_threshold=(
                float(fields["audio_verify_threshold"])
                if fields.get("audio_verify_threshold", "").strip()
                else None
            ),
            enabled=fields.get("enabled") == "on",
        ).to_config()
        collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
        collections.append(config)
        repo.save_all(collections)
    except ValidationError as exc:
        return _flash(
            "/ui/collections", f"Error al actualizar: {_validation_message(exc)}", "error"
        )
    except Exception as exc:
        return _flash("/ui/collections", f"Error al actualizar: {exc}", "error")
    return _flash("/ui/collections", f"Colección '{new_name}' actualizada")


@router.post("/ui/collections/{name}/delete", include_in_schema=False)
async def ui_delete_collection(name: str) -> RedirectResponse:
    """Delete a collection via the GUI.

    Args:
        name: Collection name.

    Returns:
        Redirect to the collections page with a flash.
    """
    repo = CollectionsRepository()
    if repo.get(name) is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
    repo.save_all(collections)
    return _flash("/ui/collections", f"Colección '{name}' eliminada")


@router.post("/ui/collections/{name}/run", include_in_schema=False)
async def ui_run_collection(name: str) -> RedirectResponse:
    """Trigger a collection run immediately.

    If a workflow for the collection is already running, does not start a new
    one and flashes a notice instead.

    Args:
        name: Collection name.

    Returns:
        Redirect to the collections page with a flash.
    """
    repo = CollectionsRepository()
    collection = repo.get(name)
    if collection is None:
        return _flash("/ui/collections", f"Colección '{name}' no encontrada", "error")
    service = TemporalAdminService()
    try:
        running = await service.is_collection_running(collection.name)
    except Exception as exc:
        logger.error("ui_run_check_failed", collection=name, error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al comprobar la ejecución: {exc}", "error")
    if running:
        return _flash("/ui/collections", f"Ya hay una ejecución en curso para '{name}'", "warn")
    try:
        workflow_id = await service.trigger_collection(collection)
    except Exception as exc:
        logger.error("ui_run_failed", collection=name, error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al lanzar: {exc}", "error")
    return _flash("/ui/collections", f"Workflow lanzado: {workflow_id}")


@router.post("/ui/schedules/resync", include_in_schema=False)
async def ui_resync_schedules() -> RedirectResponse:
    """Resync Temporal schedules with collections.toml.

    Returns:
        Redirect to the collections page with a flash.
    """
    try:
        result = await TemporalAdminService().resync_schedules(CollectionsRepository().list())
        message = (
            f"Schedules resincronizados: {len(result['created_or_updated'])} creados/actualizados, "
            f"{len(result['removed'])} eliminados"
        )
    except Exception as exc:
        logger.error("ui_resync_failed", error=str(exc), exc_info=True)
        return _flash("/ui/collections", f"Fallo al resincronizar: {exc}", "error")
    return _flash("/ui/collections", message)


@router.post("/ui/voices/clone", include_in_schema=False)
async def ui_clone_voice(request: Request) -> RedirectResponse:
    """Clone a voice from a base64-encoded audio reference.

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the voices page with a flash.
    """
    import base64

    fields = await _form(request)
    name = fields.get("name", "").strip()
    audio_base64 = fields.get("audio_base64", "")
    try:
        payload = base64.b64decode(audio_base64, validate=True)
        await VoicesService().clone_voice(
            name,
            fields.get("language", "es"),
            payload,
            filename=fields.get("filename", "reference.wav"),
            ref_text=fields.get("ref_text", "").strip() or None,
        )
    except Exception as exc:
        return _flash("/ui/voices", f"Fallo al clonar voz: {exc}", "error")
    return _flash("/ui/voices", f"Voz '{name}' clonada")


@router.post("/ui/voices/custom", include_in_schema=False)
async def ui_create_custom_voice(request: Request) -> RedirectResponse:
    """Create an operator-authored voice profile from the GUI form (Phase 9.3).

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        Redirect to the voices page with a flash describing the outcome.
    """
    from src.core.voice_catalog import VoiceCatalogError

    fields = await _form(request)
    name = fields.get("name", "").strip()
    lines = [line.strip() for line in fields.get("ref_text", "").splitlines() if line.strip()]
    if not name:
        return _flash("/ui/voices", "Nombre de voz requerido", "error")
    if not lines:
        return _flash("/ui/voices", "El texto de referencia es obligatorio", "error")
    try:
        payload = CustomVoiceIn(
            name=name,
            label=fields.get("label", ""),
            language=fields.get("language", "es"),
            instruct=fields.get("instruct", ""),
            ref_segments=lines,
            role_hint=fields.get("role_hint", ""),
            generate=fields.get("generate", "") == "on",
        )
        result = await voice_catalog_admin.create_custom_voice(payload)
    except ValidationError as exc:
        return _flash("/ui/voices", f"Voz no válida: {_validation_message(exc)}", "error")
    except VoiceCatalogError as exc:
        return _flash("/ui/voices", str(exc), "error")
    saved = result["profile"].name
    if result["start_error"]:
        return _flash(
            "/ui/voices",
            f"Voz '{saved}' creada, pero no se pudo encolar el diseño: {result['start_error']}",
            "warn",
        )
    if result["workflow_id"]:
        return _flash(
            "/ui/voices",
            f"Voz '{saved}' creada; diseño en cola ({result['workflow_id']})",
            "ok",
        )
    return _flash("/ui/voices", f"Voz '{saved}' creada (pendiente de generar)", "ok")


@router.get("/ui/fragments/voices_generated", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_voices_generated(request: Request) -> HTMLResponse:
    """Rows of the "Voces generadas" card, polled while designs run (Phase 9.3).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the generated-voices table fragment.
    """
    from src.core.voice_catalog import VoiceCatalogError

    try:
        voices = await voice_catalog_admin.generated_voices()
        error = ""
    except VoiceCatalogError as exc:
        voices = []
        error = str(exc)
    return _render(request, "partials/_voices_generated.html", {"voices": voices, "error": error})


@router.get("/ui/voices/audio/{name}", include_in_schema=False)
async def ui_voice_audio(name: str) -> Response:
    """Serve the reference WAV of one catalog voice (Phase 9.3).

    The gateway owns the reference since P6, so the bytes are fetched from it
    and streamed back as they arrive; news-engine keeps no ``refs/`` directory.

    Args:
        name: Profile name, optionally with a ``.wav`` suffix (the audio tag
            appends one).

    Returns:
        The WAV bytes with an ``audio/wav`` content type.

    Raises:
        HTTPException: 404 for an unknown voice, an unsafe name, or a voice
            whose reference has not been generated.
    """
    from fastapi import HTTPException

    wav = await voice_catalog_admin.reference_audio(name)
    if wav is None:
        raise HTTPException(status_code=404, detail="voice not found")
    return Response(content=wav, media_type="audio/wav")


@router.post("/ui/voices/test", response_class=HTMLResponse, include_in_schema=False)
async def ui_test_voice(request: Request) -> HTMLResponse:
    """Synthesize (or reuse) a short sample of one voice (Phase 9.4).

    HTMX posts the picker form and swaps this fragment into the player box,
    so a failure reads as an inline message instead of a redirect flash.

    Args:
        request: Incoming request with an `application/x-www-form-urlencoded` body.

    Returns:
        HTMLResponse with ``partials/_voice_sample.html``.
    """
    fields = await _form(request)
    voice = fields.get("voice", "").strip()
    try:
        result = await voice_preview.preview(voice)
    except voice_preview.VoicePreviewError as exc:
        return _render(
            request,
            "partials/_voice_sample.html",
            {"voice": voice, "error": str(exc)},
        )
    return _render(request, "partials/_voice_sample.html", result)


@router.get("/ui/voices/sample/{filename}", include_in_schema=False)
async def ui_voice_sample(filename: str) -> FileResponse:
    """Serve a cached preview sample (Phase 9.4).

    Args:
        filename: Sample file name as linked by the preview fragment.

    Returns:
        FileResponse with the cached audio bytes.

    Raises:
        HTTPException: 404 when the name is unsafe or the sample is missing.
    """
    from fastapi import HTTPException

    path = voice_preview.cached_sample(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="sample not found")
    return FileResponse(path, media_type=voice_preview.media_type_for(filename), filename=filename)


async def _session_response(
    identity: str, role: Role, user_id: int | None = None
) -> RedirectResponse:
    """Start a server-side session and redirect to the landing page.

    Args:
        identity: Display identity (email or admin username).
        role: Role stored with the session.
        user_id: users.id for non-admin sessions, None for the admin.

    Returns:
        A 302 to /ui/collections carrying the HttpOnly session cookie.
    """
    from src.api.auth import ADMIN_COOKIE_NAME, SESSION_TTL_HOURS, create_session

    token = await create_session(identity, role, user_id=user_id)
    response = RedirectResponse("/ui/collections", status_code=302)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_HOURS * 60 * 60,
    )
    return response


@router.get("/ui/login", response_class=HTMLResponse, include_in_schema=False)
async def ui_login_page(request: Request) -> HTMLResponse:
    """Render the login page (admin credentials and/or email-code entry).

    Args:
        request: Incoming request; ``?step=code&email=...`` selects the
            one-time-code form after a code was emailed.

    Returns:
        HTMLResponse for the login page.
    """
    from src.core.config import settings

    use_legacy_token = bool(settings.admin_api_token) and not settings.admin_username
    verify_email = request.query_params.get("email", "")
    return _render(
        request,
        "login.html",
        {
            "use_legacy_token": use_legacy_token,
            "verify_email": verify_email,
            "show_code_step": request.query_params.get("step") == "code",
        },
    )


@router.get("/ui/register", response_class=HTMLResponse, include_in_schema=False)
async def ui_register_page(request: Request) -> HTMLResponse:
    """Render the self-registration page (centered, nav hidden).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the registration form.
    """
    return _render(request, "register.html", {"active": "login"})


@router.post("/ui/register", include_in_schema=False)
async def ui_register(request: Request) -> RedirectResponse:
    """Create a pending account from the registration form (Phase 6).

    The account starts ``pending``: the admin approves it and assigns the
    role from the /ui/users page before the owner can request a code.

    Args:
        request: Incoming request with the ``email`` field.

    Returns:
        Redirect back to the login page with a flash message.
    """
    from src.api.db.database import UsersRepository

    fields = await _form(request)
    email = fields.get("email", "").strip()
    if not email or email.count("@") != 1 or "." not in email.split("@")[-1]:
        return _flash("/ui/register", "Email no válido", "error")
    try:
        await UsersRepository().create(email, role="viewer")
    except ValueError:
        return _flash(
            "/ui/register",
            "Ese email ya está registrado. Inicia sesión con un código.",
            "error",
        )
    logger.info("user_registered", email=email)
    return _flash(
        "/ui/login",
        "Registro recibido. El administrador validará tu cuenta.",
        "ok",
    )


@router.get("/ui/users", response_class=HTMLResponse, include_in_schema=False)
async def ui_users(request: Request) -> HTMLResponse:
    """Render the user management page (admin only, Phase 6).

    The local admin credential never appears here: the table lists only
    self-registered accounts with their status and assigned role.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the users page.
    """
    users = await UsersRepository().list_all()
    return _render(
        request,
        "users.html",
        {"users": users, "assignable_roles": ASSIGNABLE_ROLES},
    )


@router.post("/ui/users/{user_id}/approve", include_in_schema=False)
async def ui_user_approve(user_id: int, request: Request) -> RedirectResponse:
    """Approve a pending account and assign its role in one step.

    Args:
        user_id: Primary key of the user to approve.
        request: Incoming request with the ``role`` field.

    Returns:
        Redirect to /ui/users with a flash message.
    """
    fields = await _form(request)
    role = fields.get("role", "")
    if role not in ASSIGNABLE_ROLES:
        return _flash("/ui/users", "Rol no válido", "error")
    try:
        await UsersRepository().set_status(user_id, "active", role)
    except ValueError:
        return _flash("/ui/users", "Usuario no encontrado", "error")
    logger.info("user_approved", user_id=user_id, role=role)
    return _flash("/ui/users", f"Usuario aprobado con rol '{role}'")


@router.post("/ui/users/{user_id}/role", include_in_schema=False)
async def ui_user_role(user_id: int, request: Request) -> RedirectResponse:
    """Change the role of an existing account, keeping its status.

    Args:
        user_id: Primary key of the user to update.
        request: Incoming request with the ``role`` field.

    Returns:
        Redirect to /ui/users with a flash message.
    """
    fields = await _form(request)
    role = fields.get("role", "")
    if role not in ASSIGNABLE_ROLES:
        return _flash("/ui/users", "Rol no válido", "error")
    repo = UsersRepository()
    user = await repo.get(user_id)
    if user is None:
        return _flash("/ui/users", "Usuario no encontrado", "error")
    await repo.set_status(user_id, user["status"], role)
    logger.info("user_role_changed", user_id=user_id, role=role)
    return _flash("/ui/users", f"Rol actualizado a '{role}'")


@router.post("/ui/users/{user_id}/disable", include_in_schema=False)
async def ui_user_disable(user_id: int) -> RedirectResponse:
    """Disable an account; open sessions are purged by the repository.

    Args:
        user_id: Primary key of the user to disable.

    Returns:
        Redirect to /ui/users with a flash message.
    """
    try:
        await UsersRepository().set_status(user_id, "disabled")
    except ValueError:
        return _flash("/ui/users", "Usuario no encontrado", "error")
    logger.info("user_disabled", user_id=user_id)
    return _flash("/ui/users", "Usuario deshababilitado")


@router.post("/ui/users/{user_id}/enable", include_in_schema=False)
async def ui_user_enable(user_id: int) -> RedirectResponse:
    """Re-enable a previously disabled account.

    Args:
        user_id: Primary key of the user to enable.

    Returns:
        Redirect to /ui/users with a flash message.
    """
    try:
        await UsersRepository().set_status(user_id, "active")
    except ValueError:
        return _flash("/ui/users", "Usuario no encontrado", "error")
    logger.info("user_enabled", user_id=user_id)
    return _flash("/ui/users", "Usuario habilitado")


@router.post("/ui/users/{user_id}/delete", include_in_schema=False)
async def ui_user_delete(user_id: int) -> RedirectResponse:
    """Delete an account and purge its sessions.

    Args:
        user_id: Primary key of the user to delete.

    Returns:
        Redirect to /ui/users with a flash message.
    """
    try:
        await UsersRepository().delete(user_id)
    except ValueError:
        return _flash("/ui/users", "Usuario no encontrado", "error")
    logger.info("user_deleted", user_id=user_id)
    return _flash("/ui/users", "Usuario eliminado")


# ---------------------------------------------------------------------------
# Phase 7.16: phonetic-adaptation heuristics (one row per language pair)
# ---------------------------------------------------------------------------


def _heuristics_payload(fields: dict[str, str]) -> PhoneticHeuristicsIn:
    """Build the heuristics payload of the admin form.

    Textareas arrive as plain text: the model's validators accept newline- or
    comma-separated word lists and ``term = respelling`` lines (or raw JSON),
    so the form never has to emit JSON.

    Args:
        fields: Parsed form fields.

    Returns:
        The validated payload.

    Raises:
        ValidationError: On a bad code, an unparsable list or a same-language
            pair (the caller maps it onto a flash).
    """
    origin = (fields.get("language_origin", "") or "").strip().lower()
    target = (fields.get("language_target", "") or "").strip().lower()
    allowed = {"en", "es", "fr", "ca", "de"}
    if origin not in allowed or target not in allowed:
        raise ValueError("language_origin/target must be in {en,es,fr,ca,de}")
    return PhoneticHeuristicsIn(
        language_origin=origin,
        language_target=target,
        language_origin_name=fields.get("language_origin_name", "") or origin,
        language_target_name=fields.get("language_target_name", "") or target,
        suffixes=fields.get("suffixes", ""),
        digraphs=fields.get("digraphs", ""),
        tech_terms=fields.get("tech_terms", ""),
        native_words=fields.get("native_words", ""),
        examples=fields.get("examples", ""),
        ascii_only=fields.get("ascii_only") == "on",
        flag_all_foreign=fields.get("flag_all_foreign") == "on",
        enabled=fields.get("enabled") == "on",
    )


def _heuristics_form_context(row: PhoneticHeuristicsOut | None) -> dict:
    """Render-ready context of the create/edit heuristics form.

    Lists become newline-separated textarea text and the example map becomes
    ``term = respelling`` lines, so what the admin sees round-trips through
    the same parser.

    Args:
        row: Row being edited, or None for the empty create form.

    Returns:
        Context for ``phonetic_form.html``.
    """
    if row is None:
        return {
            "heuristics": None,
            "language_origin": "en",
            "language_target": "es",
            "language_origin_name": "English",
            "language_target_name": "Spanish",
            "suffixes": "",
            "digraphs": "",
            "tech_terms": "",
            "native_words": "",
            "examples": "",
            "ascii_only": True,
            "flag_all_foreign": False,
            "enabled": True,
        }
    return {
        "heuristics": row,
        "language_origin": row.language_origin,
        "language_target": row.language_target,
        "language_origin_name": row.language_origin_name,
        "language_target_name": row.language_target_name,
        "suffixes": "\n".join(row.suffixes),
        "digraphs": "\n".join(row.digraphs),
        "tech_terms": "\n".join(row.tech_terms),
        "native_words": "\n".join(row.native_words),
        "examples": "\n".join(f"{term} = {value}" for term, value in row.examples.items()),
        "ascii_only": row.ascii_only,
        "flag_all_foreign": row.flag_all_foreign,
        "enabled": row.enabled,
    }


@router.get("/ui/phonetics", response_class=HTMLResponse, include_in_schema=False)
async def ui_phonetics(request: Request) -> HTMLResponse:
    """Render the phonetic heuristics table (admin, Phase 7.16).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse listing every configured language pair.
    """
    try:
        rows = await phonetic_heuristics.list_all()
        error = ""
    except HeuristicsError as exc:
        rows, error = [], str(exc)
    return _render(request, "phonetics.html", {"heuristics": rows, "error": error})


@router.get("/ui/phonetics/new", response_class=HTMLResponse, include_in_schema=False)
async def ui_phonetics_new(request: Request) -> HTMLResponse:
    """Render the empty create form for one language pair.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the heuristics form.
    """
    return _render(request, "phonetic_form.html", _heuristics_form_context(None))


@router.post("/ui/phonetics", include_in_schema=False)
async def ui_phonetics_create(request: Request) -> RedirectResponse:
    """Create one heuristics row from the admin form.

    Args:
        request: Incoming request with the form body.

    Returns:
        Redirect to the heuristics list with a flash.
    """
    fields = await _form(request)
    try:
        payload = _heuristics_payload(fields)
        saved = await phonetic_heuristics.save(payload)
    except ValidationError as exc:
        return _flash("/ui/phonetics/new", f"Error al guardar: {_validation_message(exc)}", "error")
    except HeuristicsError as exc:
        return _flash("/ui/phonetics/new", str(exc), "error")
    logger.info(
        "ui_phonetics_created",
        origin=saved.language_origin,
        target=saved.language_target,
    )
    return _flash(
        "/ui/phonetics",
        f"Par {saved.language_origin} → {saved.language_target} guardado",
    )


@router.get("/ui/phonetics/{row_id}/edit", response_class=HTMLResponse, include_in_schema=False)
async def ui_phonetics_edit(request: Request, row_id: int) -> Response:
    """Render the edit form of one heuristics row.

    Args:
        request: Incoming request.
        row_id: Primary key of the row.

    Returns:
        HTMLResponse with the prefilled form, or a redirect when missing.
    """
    row = await phonetic_heuristics.get(row_id)
    if row is None:
        return _flash("/ui/phonetics", f"Regla {row_id} no encontrada", "error")
    return _render(request, "phonetic_form.html", _heuristics_form_context(row))


@router.post("/ui/phonetics/{row_id}/update", include_in_schema=False)
async def ui_phonetics_update(row_id: int, request: Request) -> RedirectResponse:
    """Replace one heuristics row from the admin form.

    Args:
        row_id: Primary key of the row.
        request: Incoming request with the form body.

    Returns:
        Redirect to the heuristics list with a flash.
    """
    fields = await _form(request)
    back = f"/ui/phonetics/{row_id}/edit"
    try:
        payload = _heuristics_payload(fields)
        saved = await phonetic_heuristics.save(payload, row_id=row_id)
    except ValidationError as exc:
        return _flash(back, f"Error al guardar: {_validation_message(exc)}", "error")
    except HeuristicsError as exc:
        return _flash(back, str(exc), "error")
    logger.info(
        "ui_phonetics_updated",
        row_id=row_id,
        origin=saved.language_origin,
        target=saved.language_target,
    )
    return _flash(
        "/ui/phonetics",
        f"Par {saved.language_origin} → {saved.language_target} actualizado",
    )


@router.post("/ui/phonetics/{row_id}/delete", include_in_schema=False)
async def ui_phonetics_delete(row_id: int) -> RedirectResponse:
    """Delete one heuristics row (the respeller falls back or stops).

    Args:
        row_id: Primary key of the row.

    Returns:
        Redirect to the heuristics list with a flash.
    """
    try:
        await phonetic_heuristics.delete(row_id)
    except HeuristicsError as exc:
        return _flash("/ui/phonetics", str(exc), "error")
    logger.info("ui_phonetics_deleted", row_id=row_id)
    return _flash("/ui/phonetics", f"Regla {row_id} eliminada")


@router.post("/ui/login", include_in_schema=False)
async def ui_login(request: Request) -> RedirectResponse:
    """Validate admin credentials and start a server-side session.

    Instead of storing the raw password/token in the cookie (pre-Phase 6),
    a random 12 h session token is persisted in the ``sessions`` table and
    only that token travels in the HttpOnly cookie.

    Args:
        request: Incoming request with form fields.

    Returns:
        Redirect to /ui/collections on success, back to login on failure.
    """
    from src.api.auth import _password_ok, _token_ok
    from src.core.config import settings

    fields = await _form(request)

    # Username/password flow (preferred when ADMIN_USERNAME is set)
    if settings.admin_username:
        username = fields.get("username", "").strip()
        password = fields.get("password", "")
        if username != settings.admin_username or not _password_ok(password):
            return _flash("/ui/login", "Usuario o contraseña incorrectos", "error")
        identity = username
    else:
        # Legacy token flow
        token = fields.get("token", "").strip()
        expected = (settings.admin_api_token or "").strip()
        if not expected:
            return _flash(
                "/ui/login",
                "ADMIN_API_TOKEN no configurado. Añádelo al .env y reinicia.",
                "error",
            )
        if not _token_ok(token):
            return _flash("/ui/login", "Token incorrecto", "error")
        identity = "admin"

    return await _session_response(identity, Role.ADMIN)


@router.post("/ui/login/email", include_in_schema=False)
async def ui_login_email(request: Request) -> RedirectResponse:
    """Email a one-time login code to a registered account (Phase 6).

    Args:
        request: Incoming request with the ``email`` field.

    Returns:
        Redirect to the login page's code step, or back with an error flash.
    """
    from src.api.services.login_codes import LoginCodeError, request_code

    fields = await _form(request)
    email = fields.get("email", "").strip()
    try:
        await request_code(email)
    except LoginCodeError as exc:
        return _flash("/ui/login", str(exc), "error")
    target = f"/ui/login?{urlencode({'step': 'code', 'email': email})}"
    return _flash(target, f"Código enviado a {email}. Revisa tu correo.", "ok")


@router.post("/ui/login/verify", include_in_schema=False)
async def ui_login_verify(request: Request) -> RedirectResponse:
    """Verify a one-time code and start the user's session (Phase 6).

    Args:
        request: Incoming request with ``email`` and ``code`` fields.

    Returns:
        Redirect to /ui/collections on success, back to the code step on
        failure.
    """
    from src.api.services.login_codes import LoginCodeError, verify_code

    fields = await _form(request)
    email = fields.get("email", "").strip()
    code = fields.get("code", "").strip()
    back = f"/ui/login?{urlencode({'step': 'code', 'email': email})}"
    try:
        user = await verify_code(email, code)
    except LoginCodeError as exc:
        return _flash(back, str(exc), "error")
    return await _session_response(
        str(user["email"]), Role(str(user["role"])), user_id=int(user["id"])
    )


@router.post("/ui/logout", include_in_schema=False)
async def ui_logout(request: Request) -> RedirectResponse:
    """Destroy the session row and clear the cookie.

    Args:
        request: Incoming request (its cookie identifies the session).

    Returns:
        Redirect to the login page.
    """
    from src.api.auth import ADMIN_COOKIE_NAME, destroy_session

    await destroy_session(request.cookies.get(ADMIN_COOKIE_NAME, ""))
    response = RedirectResponse("/ui/login", status_code=302)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Phase 7.10-7.12: tools page (URL summary, document TTS, lessons, podcast)
# ---------------------------------------------------------------------------


async def _annotated_jobs(jobs: list[dict], kind: str) -> list[dict]:
    """Decorate audio_jobs rows with metadata and a signed audio link."""
    annotated: list[dict] = []
    for job in jobs:
        row = dict(job)
        meta: dict = {}
        try:
            if kind == "doc_tts":
                meta = read_job_meta(str(job.get("run_id") or ""))
            elif kind == "lesson":
                meta = read_lesson_meta(str(job.get("run_id") or ""))
            elif kind == "audiobook":
                meta = read_job_meta(str(job.get("run_id") or ""))
        except Exception:  # noqa: BLE001
            meta = {}
        row["title"] = str(meta.get("title") or "")
        row["mode"] = str(meta.get("mode") or "")
        row["char_count"] = meta.get("char_count")
        row["duration"] = meta.get("duration_seconds")
        estimate = meta.get("estimate") if isinstance(meta.get("estimate"), dict) else {}
        progress = meta.get("progress") if isinstance(meta.get("progress"), dict) else {}
        row["estimate"] = (estimate or {}).get("wall_seconds")
        row["eta"] = eta_seconds(estimate, progress)
        row["chunks_done"] = int((progress or {}).get("done") or 0)
        row["chunks_total"] = int((progress or {}).get("total") or 0)
        row["audio_url"] = None
        if job.get("status") == "FINISHED_OK" and job.get("audio_path"):
            run_id = str(job.get("run_id") or "")
            try:
                if kind == "lesson":
                    row["audio_url"] = lesson_audio_link(run_id)
                elif kind == "narration":
                    row["audio_url"] = build_signed_path(run_id, prefix=NARRATION_AUDIO_LINK_PREFIX)
                elif kind == "audiobook":
                    row["audio_url"] = f"/api/admin/audiobooks/{run_id}/download"
                else:
                    row["audio_url"] = build_signed_path(run_id)
            except SignedLinkError:
                row["audio_url"] = None
        annotated.append(row)
    return annotated


@router.get("/ui/tools", response_class=HTMLResponse, include_in_schema=False)
async def ui_tools(request: Request) -> HTMLResponse:
    """Render the tools page: URL summary, documents, lessons and podcast.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the four forms: the document TTS voice is a
        Phase 9.7 picker over the edge vocabulary, the lessons one over the
        whole registry.
    """
    omni = await _omni_options()
    edge = await _edge_voice_options()
    all_voices = [voice.model_dump() for voice in await list_all_voices()]
    from src.core.config import get_settings

    doc_picker = _picker_context(
        edge["voices"],
        picker_id="doc",
        field="voice",
        engines="edge",
        empty_label="(por defecto)",
        langs=False,
        lang_input="[name='language']",
    )
    return _render(
        request,
        "tools.html",
        {
            "collections": _collections(),
            "omni_languages": omni["languages"],
            "doc_picker": doc_picker,
            "doc_language_options": voice_search.language_options(edge["voices"]),
            "lesson_language_options": voice_search.language_options(all_voices),
            # Phase 7.16: the lesson form only offers the adaptation when the
            # teacher feature can actually run it.
            "teacher_respelling_enabled": get_settings().teacher_respelling_enabled,
            **_lesson_voice_context(
                all_voices,
                native_language="es",
                target_language="en",
            ),
        },
    )


@router.post("/ui/tools/url", include_in_schema=False)
async def ui_tools_url(request: Request) -> RedirectResponse:
    """Start a single-URL summary (Phase 7.10) from the tools page.

    Args:
        request: The form body (url, collection, store, deliver).

    Returns:
        Redirect back to the tools page with a flash.
    """
    target = "/ui/tools#tool-url"
    fields = await _form(request)
    url = (fields.get("url") or "").strip()
    if not url:
        return _flash(target, "La URL es obligatoria", "error")
    name = (fields.get("collection") or "default").strip()
    collection = CollectionsRepository().resolve(name)
    if collection is None:
        return _flash(target, f"Colección '{name}' no encontrada", "error")
    try:
        workflow_id = await TemporalAdminService().start_url_summarize(
            collection,
            url=url,
            store=fields.get("store") == "on",
            deliver=fields.get("deliver") == "on",
        )
    except Exception as exc:
        logger.error("ui_url_failed", url=url, error=str(exc), exc_info=True)
        return _flash(target, f"No se pudo lanzar: {exc}", "error")
    logger.info("ui_url_started", workflow_id=workflow_id, collection=collection.name)
    return _flash(target, f"Resumen de URL lanzado: {workflow_id}", "ok")


@router.post("/ui/tools/doc-tts", include_in_schema=False)
async def ui_tools_doc_tts(request: Request) -> RedirectResponse:
    """Queue a document for TTS (Phase 7.11) from the tools page.

    Args:
        request: The multipart form body (file, title, voice, language).

    Returns:
        Redirect back to the tools page with a flash.
    """
    target = "/ui/tools#tool-doc"
    fields, upload = await _file_form(request)
    if upload is None:
        return _flash(target, "Falta el fichero", "error")
    try:
        result = await submit_document(
            upload=upload,
            content_length=request.headers.get("content-length", ""),
            voice=fields.get("voice") or None,
            language=fields.get("language") or None,
            title=fields.get("title") or None,
        )
    except SubmitError as exc:
        return _flash(target, exc.message, "error")
    except Exception as exc:
        logger.error("ui_doc_tts_failed", error=str(exc), exc_info=True)
        return _flash(target, f"No se pudo lanzar: {exc}", "error")
    return _flash(
        target,
        f"Documento en cola: {result['workflow_id']} ({result['title']})",
        "ok",
    )


@router.post("/ui/tools/lessons", include_in_schema=False)
async def ui_tools_lessons(request: Request) -> RedirectResponse:
    """Queue a language lesson (Phase 7.12) from the tools page.

    Args:
        request: The multipart form body (file, mode, languages, collection...).

    Returns:
        Redirect back to the tools page with a flash.
    """
    target = "/ui/tools#tool-lesson"
    fields, upload = await _file_form(request)
    if upload is None:
        return _flash(target, "Falta el fichero de apuntes", "error")
    try:
        result = await submit_lesson(
            upload=upload,
            target_language=fields.get("target_language", ""),
            native_language=fields.get("native_language", ""),
            content_length=request.headers.get("content-length", ""),
            mode=fields.get("mode", "mixed"),
            collection=fields.get("collection", "default"),
            title=fields.get("title", ""),
            spec=fields.get("spec") or None,
            voice=fields.get("voice") or None,
            voice2=fields.get("voice2") or None,
            phonetic_adaptation=fields.get("phonetic_adaptation") == "on",
            run_in_modal=fields.get("narr_run_in_modal") == "on",
        )
    except SubmitError as exc:
        return _flash(target, exc.message, "error")
    except Exception as exc:
        logger.error("ui_lesson_failed", error=str(exc), exc_info=True)
        return _flash(target, f"No se pudo lanzar: {exc}", "error")
    return _flash(
        target,
        f"Lección en cola: {result['workflow_id']} ({result['title']})",
        "ok",
    )


@router.post("/ui/tools/audiobooks", include_in_schema=False)
async def ui_tools_audiobooks(request: Request) -> RedirectResponse:
    """Queue a document for audiobook M4B."""
    target = "/ui/tools#tool-audiobook"
    fields, upload = await _file_form(request)
    if upload is None:
        return _flash(target, "Falta el fichero", "error")
    try:
        from src.api.services.audiobook import submit_audiobook

        result = await submit_audiobook(
            upload=upload,
            content_length=request.headers.get("content-length", ""),
            voice=fields.get("voice") or None,
            language=fields.get("language") or None,
            title=fields.get("title") or None,
        )
        # try to save cover if provided
        try:
            from pathlib import Path

            from src.api.services.audio_files import atomic_write

            form = await request.form()
            cover = form.get("cover")
            if cover and getattr(cover, "filename", None):
                data = await cover.read()
                ext = Path(getattr(cover, "filename", "")).suffix.lower()
                out = Path("data/audiobooks/tmp") / f"{result['workflow_id']}_cover{ext}"
                out.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(out, data)
        except Exception:
            pass
    except SubmitError as exc:
        return _flash(target, exc.message, "error")
    except Exception as exc:
        logger.error("ui_audiobook_failed", error=str(exc), exc_info=True)
        return _flash(target, f"No se pudo lanzar: {exc}", "error")
    return _flash(target, f"Audiolibro en cola: {result['workflow_id']} ({result['title']})", "ok")


@router.post("/ui/tools/podcast", include_in_schema=False)
async def ui_tools_podcast(request: Request) -> RedirectResponse:
    """Render the podcast of a collection's latest digest (Phase 7.12 GUI).

    The script is generated by the LLM from that run's digest, so the render
    only starts once the collection has at least one run.

    Args:
        request: The form body (collection).

    Returns:
        Redirect back to the tools page with a flash.
    """
    target = "/ui/tools#tool-podcast"
    fields = await _form(request)
    name = (fields.get("collection") or "default").strip()
    level, message = await start_collection_podcast(name)
    return _flash(target, message, level)


@router.get("/ui/fragments/lesson_voices", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_lesson_voices(
    request: Request,
    mode: str = "",
    voice: str = "",
    voice2: str = "",
    current_mode: str = "",
    voice_q: str = "",
    voice2_q: str = "",
    native_language: str = "",
    target_language: str = "",
) -> HTMLResponse:
    """Render the voice picker(s) of the language-lesson form (Phase 7.12).

    The block is re-fetched when the mode select changes: 'dual' voices the
    class with two roles (native-language teacher + target-language teacher),
    so it must ask for two voices, while 'mixed' and 'native' keep the single
    one. The current selections ride along as query parameters (the mode select
    and the pickers inside ``#lesson-voices`` are htmx-included) so toggling
    the mode does not drop what was already picked, and the typed filters
    (glob query) keep their own parameters so a re-render does not lose them
    either.

    A pick does not carry the mode select (it lives outside the block), so
    the partial echoes the last mode in a ``current_mode`` hidden field:
    ``mode`` (sent by the mode select itself) wins when present, otherwise
    the echo is used. The response also carries the submit button
    out-of-band, disabled while the two dual voices mix engines (Phase 9.2),
    and the live cast preview of the cast avanzado box (Phase 9.8).

    Since Phase 9.8 the voice pickers are filtered by the form's own language
    fields (``native_language``/``target_language``), which ride along as
    query parameters the same way the mode does.

    Args:
        request: Incoming request.
        mode: Lesson mode sent by the mode select ('' on voice changes).
        voice: Currently selected native/single voice id.
        voice2: Currently selected target voice id (dual only).
        current_mode: Last mode echoed by the hidden field of the partial.
        voice_q: Typed glob query of the native/single picker.
        voice2_q: Typed glob query of the target picker (dual only).
        native_language: Student's native language ('es').
        target_language: Language being taught ('en').

    Returns:
        HTMLResponse with the _lesson_voices partial.
    """
    all_voices = [voice.model_dump() for voice in await list_all_voices()]
    return _render(
        request,
        "partials/_lesson_voices.html",
        _lesson_voice_context(
            all_voices,
            mode=mode or current_mode,
            voice=voice,
            voice2=voice2,
            fragment=True,
            voice_q=voice_q,
            voice2_q=voice2_q,
            native_language=native_language,
            target_language=target_language,
        ),
    )


@router.get("/ui/fragments/voice_picker", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_voice_picker(
    request: Request,
    field: str = "voice",
    voice: str = "",
    voice2: str = "",
    q: str = "",
    lang: str = "",
    engines: str = "all",
    empty_label: str = "",
    language: str = "",
    mode: str = "",
    native_language: str = "",
    target_language: str = "",
) -> HTMLResponse:
    """Search results of one tools voice picker (Phase 9.7/9.8).

    Fired by the query field (keyup debounce) and the language-driven sources
    of the forms; the response swaps only the radio results, so the search
    row keeps its state. The hidden inputs of the picker wrapper
    (hx-included) tell which radio group, vocabulary and empty option to
    render, and the currently checked radio — carried by the same include —
    is always kept in the response, checked.

    Since Phase 9.8 the language filter comes from the form's own language
    fields instead of an internal select: the document form sends ``language``
    and the lesson form sends ``native_language``/``target_language`` plus its
    ``mode``, and this route maps them to a single ``lang`` prefix, so one
    filter drives the voices that language can actually voice (a lesson in
    'dual' filters the native picker by ``native_language`` and the target
    picker by ``target_language``; mixed/native by the target language alone).

    Args:
        request: Incoming request.
        field: Radio group name ('voice' or 'voice2'); anything else falls
            back to 'voice'.
        voice: Currently checked voice (native/single/doc picker).
        voice2: Currently checked voice (target picker, dual mode only).
        q: Glob-style query over id, label, language, category and engines.
        lang: Legacy language prefix param, kept for tests ('' normally).
        engines: 'all' or the canonical engine of the vocabulary (the doc
            picker sends 'edge').
        empty_label: Label of the value='' option ('' omits it).
        language: Document form language field ('es' matches es and es-ES).
        mode: Lesson mode driving the per-role language mapping.
        native_language: Lesson native language (native picker, dual only).
        target_language: Lesson target language (single/target picker).

    Returns:
        HTMLResponse with the _voice_picker_results partial.
    """
    wanted_field = field if field in {"voice", "voice2"} else "voice"
    current = voice2 if wanted_field == "voice2" else voice
    engine = engines.strip() if engines not in {"", "all"} else None
    resolved = _picker_lang_param(
        field=wanted_field,
        lang=lang,
        language=language,
        mode=mode,
        native_language=native_language,
        target_language=target_language,
    )
    voices = [entry.model_dump() for entry in await list_all_voices(engine=engine)]
    context = _picker_results_context(
        voices, field=wanted_field, current=current, q=q, lang=resolved
    )
    context["empty_label"] = empty_label
    return _render(request, "partials/_voice_picker_results.html", {"picker": context})


@router.get("/ui/fragments/tool_jobs", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_tool_jobs(request: Request, kind: str = "doc_tts") -> HTMLResponse:
    """Poll the job tables of the tools page (htmx, every 10 s).

    Args:
        request: Incoming request.
        kind: 'doc_tts', 'lesson', 'narration' (audio_jobs) or 'url'
            (digest_runs filtered to url-summary workflow ids).

    Returns:
        HTMLResponse fragment with the rows for the requested kind.
    """
    context: dict = {
        "kind": kind,
        "status_labels": JOB_STATUS_LABELS,
        "status_badges": JOB_STATUS_BADGES,
        "run_labels": STATUS_LABELS,
        "run_badges": RUN_BADGES,
        "fmt_iso": _fmt_iso,
        "jobs": [],
        "url_rows": [],
        "error": None,
    }
    if kind == "url":
        runs = await RunsRepository().list_runs(limit=100)
        context["url_rows"] = [
            r for r in runs if str(r.get("workflow_id", "")).startswith("url-summary-")
        ][:20]
    elif kind in ("doc_tts", "lesson", "narration"):
        if kind == "audiobook":
            jobs = await AudioJobsRepository().list_jobs(kind="audiobook")
        else:
            jobs = await AudioJobsRepository().list_jobs(kind=kind)
        context["jobs"] = await _annotated_jobs(jobs[:20], kind)
    else:
        context["error"] = f"tipo desconocido: {kind}"
    return _render(request, "partials/_tool_jobs.html", context)


@router.post("/ui/tools/jobs/{kind}/{run_id}/delete", include_in_schema=False)
async def ui_tools_delete_job(request: Request, kind: str, run_id: str) -> RedirectResponse:
    """Delete a generated audio and everything the run left behind (Phase 9.8).

    Only the artifacts and the ``audio_jobs`` row die here — not the Temporal
    workflow, whose history stays for auditing; a deleted file makes any old
    signed link 404, which is the point of the button. Narrations also forget
    their ``narration_path`` so the collections page stops pointing at a file
    that no longer exists.

    Args:
        request: Incoming request.
        kind: Audio job kind: 'doc_tts', 'lesson' or 'narration'.
        run_id: Workflow id of the run to delete.

    Returns:
        Redirect back to the tools page with a flash.
    """
    target = "/ui/tools"
    try:
        job = await AudioJobsRepository().get_job(run_id, kind=kind)
        if job is None:
            return _flash(target, "no hay ningún trabajo con ese id", "error")
        deleted = delete_job_artifacts(run_id, kind, str(job.get("audio_path") or ""))
        await AudioJobsRepository().delete_job(run_id, kind)
        if kind == "narration":
            await RunsRepository().set_narration_path(run_id, None)
    except (ValueError, LessonError) as exc:
        return _flash(target, str(exc), "error")
    except Exception as exc:
        logger.error("ui_tools_delete_failed", kind=kind, run_id=run_id, error=str(exc))
        return _flash(target, f"No se pudo borrar: {exc}", "error")
    files = " + ".join(deleted) or "sin ficheros en disco"
    return _flash(target, f"Borrado {kind} {run_id}: {files}", "ok")


@router.post("/ui/collections/{name}/narrate", include_in_schema=False)
async def ui_narrate_collection(name: str) -> RedirectResponse:
    """Render a collection's podcast from the collections page button.

    Args:
        name: Collection name from the URL path.

    Returns:
        Redirect to the collections page with a flash.
    """
    level, message = await start_collection_podcast(name)
    return _flash("/ui/collections", message, level)


# ---------------------------------------------------------------------------
# Phase 7.13+ / GUI: .env settings editor (schema derived from Settings)
# ---------------------------------------------------------------------------


@router.get("/ui/settings", response_class=HTMLResponse, include_in_schema=False)
async def ui_settings(request: Request) -> HTMLResponse:
    """Render the .env editor: every setting, grouped by section.

    Secrets are shown as inputs without a value; the placeholder says whether
    one is stored.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the settings page.
    """
    fields = build_schema()
    return _render(
        request,
        "settings.html",
        {"sections": env_editor.form_context(fields), "errors": [], "submitted": {}},
    )


@router.post("/ui/settings", include_in_schema=False)
async def ui_settings_save(request: Request) -> HTMLResponse:
    """Validate and persist the edited .env values.

    The whole form is validated through the real ``Settings`` model: if any
    field is rejected **nothing is written** and the page comes back with the
    per-field errors; only changed keys touch the file.

    Args:
        request: The form body (``env_<NAME>`` fields).

    Returns:
        HTMLResponse: the settings page with a flash (save) or the errors.
    """
    fields = build_schema()
    raw = await _form(request)
    submitted = {key[4:]: value for key, value in raw.items() if key.startswith("env_")}
    current = env_editor.effective_values(fields)
    changes = env_editor.compute_changes(fields, submitted, current)
    if not changes:
        return _render(
            request,
            "settings.html",
            {
                "sections": env_editor.form_context(fields, submitted=submitted),
                "errors": [],
                "submitted": submitted,
                "flash": "Sin cambios que guardar.",
                "flash_level": "warn",
            },
        )
    _, problems = env_editor.validate_changes(fields, changes)
    if problems:
        logger.warning(
            "settings_rejected", problems=[f"{p.env_name}: {p.message}" for p in problems]
        )
        return _render(
            request,
            "settings.html",
            {
                "sections": env_editor.form_context(fields, submitted=submitted, problems=problems),
                "errors": problems,
                "submitted": submitted,
            },
        )
    try:
        env_editor.save_env(env_editor.ENV_FILE, changes)
    except OSError as exc:
        logger.error("settings_save_failed", error=str(exc), exc_info=True)
        return _render(
            request,
            "settings.html",
            {
                "sections": env_editor.form_context(fields, submitted=submitted),
                "errors": [env_editor.ValidationProblem("-", f"no se pudo escribir: {exc}")],
                "submitted": submitted,
            },
        )
    logger.info("settings_saved", changes=sorted(changes))
    return _render(
        request,
        "settings.html",
        {
            "sections": env_editor.form_context(fields),
            "errors": [],
            "submitted": {},
            "flash": env_editor.saved_flash(changes),
            "flash_level": "ok",
        },
    )


def _log_filters(request: Request) -> dict:
    """Extract the shared log filters from the request query string.

    Args:
        request: Incoming request.

    Returns:
        Keyword arguments for :func:`src.core.logging.query_logs`.
    """
    qp = request.query_params
    return {
        "levels": qp.getlist("level") or None,
        "components": qp.getlist("component") or None,
        "text": qp.get("text") or None,
        "correlation_id": qp.get("correlation_id") or None,
        "trace_id": qp.get("trace_id") or None,
    }


@router.get("/ui/logs", response_class=HTMLResponse, include_in_schema=False)
async def ui_logs(request: Request) -> HTMLResponse:
    """Render the observability console (logs, levels and traces).

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse for the observability page.
    """
    return _render(
        request,
        "logs.html",
        {
            "components": logging_ext.list_components(),
            "levels": logging_ext.get_component_levels(),
            "level_names": LOG_LEVEL_NAMES,
            "file_state": logging_ext.file_sink_state(),
        },
    )


@router.get("/ui/fragments/logs", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_logs(request: Request) -> HTMLResponse:
    """Render the filtered log table fragment for HTMX polling.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the log table.
    """
    limit = int(request.query_params.get("limit", "200") or "200")
    items = logging_ext.query_logs(limit=limit, include_file=True, **_log_filters(request))
    return _render(request, "partials/_logs.html", {"items": items})


@router.get("/ui/fragments/log_levels", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_log_levels(request: Request) -> HTMLResponse:
    """Render the per-component log level controls.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the level controls table.
    """
    return _render(
        request,
        "partials/_log_levels.html",
        {
            "components": logging_ext.list_components(),
            "levels": logging_ext.get_component_levels(),
            "level_names": LOG_LEVEL_NAMES,
        },
    )


@router.post("/ui/fragments/log_level", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_set_log_level(request: Request) -> HTMLResponse:
    """Apply a component level change and re-render the controls.

    Args:
        request: Incoming request with `component` and `level` form fields.

    Returns:
        HTMLResponse with the refreshed level controls table.
    """
    form = await _form(request)
    component = form.get("component", "")
    level = form.get("level", "")
    flash = ""
    if component and level:
        try:
            logging_ext.set_component_level(component, level)
            flash = f"{component} → {level.upper()}"
        except ValueError as exc:
            logger.warning("ui_set_log_level_invalid", component=component, error=str(exc))
    return _render(
        request,
        "partials/_log_levels.html",
        {
            "components": logging_ext.list_components(),
            "levels": logging_ext.get_component_levels(),
            "level_names": LOG_LEVEL_NAMES,
            "flash": flash,
        },
    )


@router.get("/ui/fragments/export", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_export(request: Request) -> HTMLResponse:
    """Export the current filtered view to a file and report the path.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse describing the exported file.
    """
    fmt = request.query_params.get("fmt", "jsonl")
    fmt = fmt if fmt in ("jsonl", "txt") else "jsonl"
    records = logging_ext.query_logs(limit=20000, include_file=True, **_log_filters(request))
    result = logging_ext.export_logs(records, fmt=fmt)
    return _render(request, "partials/_export.html", result)


@router.get("/ui/fragments/traces", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_traces(request: Request) -> HTMLResponse:
    """Render the recent-traces table fragment.

    Args:
        request: Incoming request.

    Returns:
        HTMLResponse with the traces table.
    """
    return _render(request, "partials/_traces.html", {"traces": tracing.summarize_traces(50)})


@router.get("/ui/fragments/trace_detail", response_class=HTMLResponse, include_in_schema=False)
async def ui_fragment_trace_detail(request: Request, trace_id: str) -> HTMLResponse:
    """Render the span timeline and linked logs for one trace.

    Args:
        request: Incoming request.
        trace_id: Trace id to inspect.

    Returns:
        HTMLResponse with the trace detail.
    """
    spans = tracing.get_inmem_spans(trace_id=trace_id, limit=1000)
    logs = logging_ext.query_logs(trace_id=trace_id, limit=500, include_file=True)
    return _render(
        request,
        "partials/_trace_detail.html",
        {"trace_id": trace_id, "spans": spans, "logs": logs},
    )
