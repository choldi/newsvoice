"""Language-teacher lesson helpers (Phase 7.12).

Turns uploaded notes into an LLM lesson (readable Markdown plus a spoken
script) and prepares it for rendering: the same ``NarrationSpec`` the rest of
the project uses, so the class is voiced by the operator's cloned voices.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import ValidationError

from src.api.models.lesson import LessonContent, LessonMode
from src.api.models.narration import NarrationScript, NarrationSpec
from src.api.services.audio_files import atomic_write
from src.api.services.signed_links import LESSON_AUDIO_LINK_PREFIX, build_signed_path
from src.core.config import get_settings
from src.core.prompts import LESSON_USER_PROMPT_TEMPLATE, build_lesson_system_prompt

logger = structlog.get_logger(__name__)

LESSON_EXTENSIONS = {".md": "md", ".txt": "txt"}
# Workflow ids are ours ("lesson-<hex>"); they become file paths, so anything
# outside this alphabet is rejected before it reaches the filesystem.
_LESSON_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")


class LessonError(Exception):
    """A permanent failure: unusable notes, an invalid reply or a bad mode."""


@dataclass(frozen=True)
class LessonNotes:
    """Decoded notes handed to the LLM."""

    text: str
    kind: str
    char_count: int


def validate_lesson_id(workflow_id: str) -> str:
    """Validate a lesson workflow id before it is used in a path.

    Args:
        workflow_id: Workflow id of the lesson.

    Returns:
        The validated id.

    Raises:
        LessonError: When the id is empty or contains foreign characters.
    """
    if not workflow_id or not _LESSON_ID_RE.fullmatch(workflow_id):
        raise LessonError(f"invalid lesson id: {workflow_id!r}")
    return workflow_id


def _decode(data: bytes) -> str:
    """Decode notes as UTF-8, falling back to latin-1 (which never fails).

    Args:
        data: Raw upload bytes.

    Returns:
        The decoded text.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def validate_notes(data: bytes, filename: str) -> LessonNotes:
    """Validate and decode an uploaded notes file.

    Args:
        data: Raw upload bytes.
        filename: Original filename; the extension selects the kind.

    Returns:
        The decoded notes.

    Raises:
        LessonError: On an unsupported extension, an empty file or notes over
            ``teacher_max_notes_chars``.
    """
    suffix = Path(filename or "").suffix.lower()
    kind = LESSON_EXTENSIONS.get(suffix)
    if kind is None:
        supported = ", ".join(sorted(LESSON_EXTENSIONS))
        raise LessonError(f"unsupported file type '{suffix or filename}': expected {supported}")
    if not data:
        raise LessonError("uploaded file is empty")
    text = _decode(data).strip()
    if not text:
        raise LessonError("notes have no text content")
    limit = get_settings().teacher_max_notes_chars
    if len(text) > limit:
        raise LessonError(f"notes too long: {len(text)} characters (limit {limit})")
    return LessonNotes(text=text, kind=kind, char_count=len(text))


def _base_dir(base_dir: str | None = None) -> Path:
    """Return the lesson storage root (uploads/text/meta).

    Args:
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        The root path.
    """
    return Path(base_dir or get_settings().teacher_dir)


def lesson_upload_path(workflow_id: str, filename: str, base_dir: str | None = None) -> Path:
    """Return the path where the uploaded notes are stored.

    Args:
        workflow_id: Workflow id of the lesson.
        filename: Original filename (its suffix is kept).
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        Path like ``<teacher_dir>/uploads/<workflow_id>.md``.

    Raises:
        LessonError: When the id is not one of ours.
    """
    validate_lesson_id(workflow_id)
    suffix = Path(filename or "").suffix.lower() or ".txt"
    path = _base_dir(base_dir) / "uploads" / f"{workflow_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def lesson_text_path(workflow_id: str, base_dir: str | None = None) -> Path:
    """Return the path of the generated lesson Markdown (the attachment).

    Args:
        workflow_id: Workflow id of the lesson.
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        Path like ``<teacher_dir>/text/<workflow_id>.md``.

    Raises:
        LessonError: When the id is not one of ours.
    """
    validate_lesson_id(workflow_id)
    path = _base_dir(base_dir) / "text" / f"{workflow_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def lesson_script_path(workflow_id: str, base_dir: str | None = None) -> Path:
    """Return the path of the generated spoken script (JSON on disk).

    Keeping the script out of the Temporal payload bounds what crosses the
    wire: only paths travel between the activities of a lesson.

    Args:
        workflow_id: Workflow id of the lesson.
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        Path like ``<teacher_dir>/text/<workflow_id>.script.json``.

    Raises:
        LessonError: When the id is not one of ours.
    """
    validate_lesson_id(workflow_id)
    path = _base_dir(base_dir) / "text" / f"{workflow_id}.script.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def lesson_meta_path(workflow_id: str, base_dir: str | None = None) -> Path:
    """Return the metadata JSON path of one lesson.

    Args:
        workflow_id: Workflow id of the lesson.
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        Path like ``<teacher_dir>/meta/<workflow_id>.json``.

    Raises:
        LessonError: When the id is not one of ours.
    """
    validate_lesson_id(workflow_id)
    path = _base_dir(base_dir) / "meta" / f"{workflow_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_lesson_meta(workflow_id: str, base_dir: str | None = None) -> dict:
    """Read a lesson's metadata, tolerating a missing or corrupted file.

    Args:
        workflow_id: Workflow id of the lesson.
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Returns:
        The stored metadata, or an empty dict.
    """
    try:
        path = lesson_meta_path(workflow_id, base_dir)
    except LessonError:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_lesson_meta(workflow_id: str, meta: dict, base_dir: str | None = None) -> None:
    """Merge fields into a lesson's metadata atomically.

    Args:
        workflow_id: Workflow id of the lesson.
        meta: Fields to store; existing keys are overwritten.
        base_dir: Explicit root; defaults to ``settings.teacher_dir``.

    Raises:
        LessonError: When the id is not one of ours.
    """
    path = lesson_meta_path(workflow_id, base_dir)
    merged = {**read_lesson_meta(workflow_id, base_dir), **meta}
    atomic_write(path, json.dumps(merged, indent=2, sort_keys=True).encode())


def roles_summary(spec: NarrationSpec) -> str:
    """Describe the cast for the prompt: names and languages as JSON.

    Args:
        spec: The narration spec carrying the cast.

    Returns:
        Compact JSON array like ``[{"name": "profe_es", "language": "es"}, ...]``.
    """
    roles = [{"name": role.name, "language": role.language or spec.language} for role in spec.roles]
    return json.dumps(roles, ensure_ascii=False)


def validate_mode(mode: LessonMode | str, spec: NarrationSpec) -> None:
    """Check the requested mode against the cast that will voice it.

    Args:
        mode: Requested lesson mode.
        spec: The cast that will be rendered.

    Raises:
        LessonError: When 'dual' is requested with fewer than two voices.
    """
    if LessonMode(mode) is LessonMode.DUAL and len(spec.roles) < 2:
        raise LessonError("mode 'dual' needs at least two roles in the spec (one per language)")


def build_lesson_messages(
    notes: str,
    mode: LessonMode | str,
    spec: NarrationSpec,
    target_language: str,
    native_language: str,
) -> list[dict[str, str]]:
    """Build the chat messages for lesson generation.

    Args:
        notes: Uploaded notes text.
        mode: Requested lesson mode.
        spec: The cast that will voice the class.
        target_language: ISO 639-1 language being taught.
        native_language: ISO 639-1 language the student thinks in.

    Returns:
        System + user messages for ``generate_completion``.
    """
    system = build_lesson_system_prompt(
        mode=str(mode),
        roles_json=roles_summary(spec),
        target_language=target_language,
        native_language=native_language,
    )
    user = LESSON_USER_PROMPT_TEMPLATE.format(notes=notes)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_lesson_content(raw: str) -> LessonContent:
    """Parse the LLM reply into a validated lesson.

    Tolerates code fences and prose around the JSON object, because models
    occasionally decorate an otherwise correct answer.

    Args:
        raw: Raw assistant reply.

    Returns:
        The validated lesson.

    Raises:
        LessonError: When the reply is not JSON or does not match the schema.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```[A-Za-z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise LessonError("the LLM reply contains no JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise LessonError(f"the LLM reply is not valid JSON: {exc}") from exc
    try:
        return LessonContent.model_validate(payload)
    except ValidationError as exc:
        raise LessonError(f"lesson payload is invalid: {exc}") from exc


def build_script(content: LessonContent, spec: NarrationSpec) -> NarrationScript:
    """Validate the lesson segments against the cast and build the script.

    Args:
        content: Parsed lesson (markdown + segments).
        spec: The cast that will be rendered.

    Returns:
        A NarrationScript validated against the spec (role membership included).

    Raises:
        LessonError: When a segment references an unknown role or the script is
            otherwise invalid for the spec.
    """
    dump = {
        "title": content.title,
        "language": spec.language,
        "source": "llm",
        "segments": [segment.model_dump() for segment in content.segments],
    }
    try:
        return NarrationScript.model_validate(dump, context={"spec": spec})
    except ValidationError as exc:
        raise LessonError(f"lesson script does not match the cast: {exc}") from exc


def attachment_text(content: LessonContent) -> str:
    """Render the readable lesson that ships as the `.md` attachment.

    Args:
        content: Parsed lesson.

    Returns:
        Markdown starting with a title heading, newline-terminated.
    """
    markdown = content.markdown.strip()
    if content.title and not markdown.startswith("#"):
        markdown = f"# {content.title}\n\n{markdown}"
    return markdown + "\n"


def should_attach_audio(duration_seconds: float) -> bool:
    """Decide whether the audio ships as a file or as a link (Phase 7.12).

    Args:
        duration_seconds: Measured duration of the rendered lesson.

    Returns:
        True when the audio fits under ``teacher_max_audio_seconds``.
    """
    return float(duration_seconds) <= get_settings().teacher_max_audio_seconds


def lesson_audio_link(workflow_id: str) -> str:
    """Build the signed link that serves the lesson audio.

    Args:
        workflow_id: Workflow id of the finished lesson.

    Returns:
        Signed path under ``/api/audio/lesson``.

    Raises:
        SignedLinkError: When signed links are disabled (empty secret).
    """
    settings = get_settings()
    return build_signed_path(
        workflow_id,
        prefix=LESSON_AUDIO_LINK_PREFIX,
        ttl=settings.teacher_link_ttl_seconds,
    )


def save_lesson(workflow_id: str, content: LessonContent, script: NarrationScript) -> str:
    """Persist the lesson Markdown and the spoken script for the activities.

    Both files travel by path between activities: a lesson script can be large
    enough that putting it in a Temporal payload would be wasteful.

    Args:
        workflow_id: Workflow id of the lesson.
        content: Parsed lesson (drives the attachment).
        script: The validated script (validated against the cast).

    Returns:
        The Markdown attachment path.

    Raises:
        LessonError: When the id is invalid or a file cannot be written.
    """
    try:
        text_path = lesson_text_path(workflow_id)
        script_path = lesson_script_path(workflow_id)
        atomic_write(text_path, attachment_text(content).encode("utf-8"))
        atomic_write(script_path, script.model_dump_json(indent=2).encode("utf-8"))
    except (OSError, LessonError) as exc:
        raise LessonError(f"cannot store the lesson: {exc}") from exc
    logger.info(
        "lesson_saved",
        workflow_id=workflow_id,
        title=content.title,
        char_count=len(content.markdown),
        segments=len(content.segments),
    )
    return str(text_path)
