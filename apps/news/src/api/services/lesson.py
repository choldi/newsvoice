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
from starlette.datastructures import UploadFile

from src.api.db.database import AudioJobsRepository
from src.api.models.lesson import LessonContent, LessonMode
from src.api.models.narration import NarrationScript, NarrationSpec, SpeakerRole, TTSBackend
from src.api.services.audio_files import atomic_write
from src.api.services.phonetic_heuristics import HeuristicsError, require_enabled_pair
from src.api.services.signed_links import LESSON_AUDIO_LINK_PREFIX, build_signed_path
from src.api.services.submit import SubmitError, check_content_length, read_upload
from src.api.services.voice_engine import VoiceEngineError, infer_backend
from src.core.config import get_settings
from src.core.prompts import LESSON_USER_PROMPT_TEMPLATE, build_lesson_system_prompt
from src.core.tracing import trace_function

logger = structlog.get_logger(__name__)

#: ``audio_jobs.kind`` discriminator of this feature.
JOB_KIND = "lesson"
# Role names of the default two-voice cast in 'dual' mode. The prompt tells the
# LLM which language each role speaks, so the names only have to be stable.
DUAL_NATIVE_ROLE = "profe_native"
DUAL_TARGET_ROLE = "profe_target"
_LANGUAGE_RE = re.compile(r"[a-z]{2}")

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


# ---------------------------------------------------------------------------
# Submit (shared by the REST endpoint and the GUI, Phase 7.12)
# ---------------------------------------------------------------------------


def language_code(value: str, field_name: str) -> str:
    """Validate a two-letter ISO 639-1 code coming from a form field.

    Args:
        value: Raw form value.
        field_name: Field name used in the error message.

    Returns:
        The trimmed, lowercase code.

    Raises:
        SubmitError: 422 when the code is not ISO 639-1.
    """
    cleaned = (value or "").strip().lower()
    if not _LANGUAGE_RE.fullmatch(cleaned):
        raise SubmitError(
            f"{field_name} must be a two-letter ISO 639-1 code (e.g. 'es')",
            status_code=422,
        )
    return cleaned


def resolve_cast(
    raw_spec: str | None,
    voice: str | None,
    target_language: str,
    *,
    mode: LessonMode | str = LessonMode.MIXED,
    native_language: str = "",
    voice2: str | None = None,
    run_in_modal: bool = False,
) -> NarrationSpec:
    """Resolve the cast that will voice the class (spec JSON or voice fields).

    With no spec the cast follows the mode: ``dual`` needs two roles (the first
    one teaches in ``native_language``, the second one in ``target_language``),
    while ``mixed``/``native`` keep the single default role. The spec JSON, when
    given, always wins so an operator can still hand-craft the cast.

    Args:
        raw_spec: Optional NarrationSpec JSON from the form.
        voice: Voice id of the native-language role (single role otherwise).
        target_language: Language used as the spec default.
        mode: Lesson mode driving the default cast shape.
        native_language: Student's native language (the first dual role speaks it).
        voice2: Voice id of the target-language role (dual mode only).
        run_in_modal: For the QWEN3_TTS backend, run synthesis in Modal instead
            of the gateway local (default), mirroring the lesson form checkbox.

    Returns:
        The validated spec.

    Raises:
        SubmitError: 422 when the spec JSON or the voices are unusable, or when
            ``dual`` is asked for without two voices.
    """
    if raw_spec and raw_spec.strip():
        try:
            return NarrationSpec.model_validate_json(raw_spec.strip())
        except ValidationError as exc:
            raise SubmitError(
                f"invalid spec JSON: {exc.errors()[0].get('msg', str(exc))}",
                status_code=422,
            ) from exc
    try:
        lesson_mode = LessonMode(str(mode).strip().lower())
    except ValueError as exc:
        raise SubmitError(f"unknown lesson mode: {mode!r}", status_code=422) from exc
    if lesson_mode is LessonMode.DUAL:
        native_id = (voice or "").strip()
        target_id = (voice2 or "").strip()
        native_lang = (native_language or "").strip()
        missing = [
            label
            for label, value in (
                ("voice (native language)", native_id),
                ("voice2 (target language)", target_id),
                ("native_language", native_lang),
            )
            if not value
        ]
        if missing:
            raise SubmitError(
                "mode 'dual' needs two voices and both languages (missing: "
                + ", ".join(missing)
                + "); pick them in the form or send a NarrationSpec",
                status_code=422,
            )
        roles = [
            SpeakerRole(name=DUAL_NATIVE_ROLE, voice_id=native_id, language=native_lang),
            SpeakerRole(name=DUAL_TARGET_ROLE, voice_id=target_id, language=target_language),
        ]
    else:
        voice_id = (voice or "").strip() or get_settings().tts_voice
        roles = [SpeakerRole(name="teacher", voice_id=voice_id)]
    # Phase 9.2: the spec engine follows the voices that will actually speak
    # (network voices keep the gateway default, catalog names pick Modal), and
    # a selection no single engine can voice is rejected here, before any job.
    try:
        backend = infer_backend([role.voice_id for role in roles])
    except VoiceEngineError as exc:
        raise SubmitError(str(exc), status_code=422) from exc
    try:
        return NarrationSpec(
            language=target_language,
            roles=roles,
            run_in_modal=run_in_modal,
            **({"tts_backend": backend} if backend else {}),
        )
    except ValidationError as exc:
        raise SubmitError(
            f"invalid default cast: {exc.errors()[0].get('msg', str(exc))}",
            status_code=422,
        ) from exc


async def validate_phonetic_adaptation(
    phonetic_adaptation: bool,
    cast: NarrationSpec,
    target_language: str,
    native_language: str,
    mode: str = LessonMode.MIXED,
) -> None:
    """Check a request to phonetically adapt the class (Phase 7.16).

    The adaptation quotes the language being taught inside text spoken in the
    student's native language, so it needs the ``qwen3_tts`` renderer (the one
    engine the ``tts_text`` map feeds), a ``dual`` cast that actually declares
    which role teaches in the native language, and an enabled heuristics row
    for the ``target -> native`` pair. Every problem is permanent: fail here,
    before a ten-minute job queues, instead of inside the workflow.

    Args:
        phonetic_adaptation: Whether the form asked for the adaptation.
        cast: Resolved cast (its engine decides feasibility).
        target_language: ISO 639-1 language being taught.
        native_language: ISO 639-1 language the class is spoken in.
        mode: Lesson mode: mixed, dual or native.

    Raises:
        SubmitError: 403 when the feature is disabled, 422 when the languages,
            the engine, the cast or the heuristics pair cannot support it.
    """
    if not phonetic_adaptation:
        return
    settings = get_settings()
    if not settings.teacher_respelling_enabled:
        raise SubmitError(
            "Phonetic adaptation is disabled (TEACHER_RESPELLING_ENABLED=false)",
            status_code=403,
        )
    if target_language == native_language:
        raise SubmitError(
            "phonetic adaptation needs different target and native languages "
            f"(both are {target_language!r})",
            status_code=422,
        )
    if cast.tts_backend is not TTSBackend.QWEN3_TTS:
        raise SubmitError(
            f"phonetic adaptation requires the qwen3_tts backend (the cast uses "
            f"{cast.tts_backend}): pick Qwen3-TTS voices or drop the option",
            status_code=422,
        )
    if LessonMode(mode) is LessonMode.DUAL:
        from src.core.respelling import dual_lacks_native_role

        if dual_lacks_native_role(cast, native_language):
            raise SubmitError(
                "mode 'dual' adapts the role that speaks the native language, but "
                f"the spec declares no {native_language!r} role: set language="
                f"'{native_language}' on the teaching role or drop the adaptation",
                status_code=422,
            )
    try:
        await require_enabled_pair(target_language, native_language)
    except HeuristicsError as exc:
        raise SubmitError(str(exc), status_code=422) from exc


def collection_delivery(collection_name: str) -> tuple[list[str], str | None, list[str]]:
    """Resolve where a lesson is delivered for a collection (Phase 7.12).

    Unknown collections fall back to Telegram with the global chat id, which
    is what the digest pipeline does for the implicit default collection.

    Args:
        collection_name: Name of the configured collection.

    Returns:
        Tuple of (delivery_channels, chat_id, email_recipients).
    """
    from src.api.services.collections_repo import CollectionsRepository

    collection = CollectionsRepository().get(collection_name)
    if collection is None:
        return ["telegram"], None, []
    return (
        list(collection.delivery_channels),
        collection.chat_id,
        list(collection.email_recipients),
    )


@trace_function(component="api.services.lesson")
async def submit_lesson(
    *,
    upload: UploadFile,
    target_language: str,
    native_language: str,
    content_length: str = "",
    mode: str = "mixed",
    collection: str = "default",
    title: str = "",
    spec: str | None = None,
    voice: str | None = None,
    voice2: str | None = None,
    phonetic_adaptation: bool = False,
    run_in_modal: bool = False,
) -> dict[str, str]:
    """Validate, store and start a lesson job (Phase 7.12).

    The REST endpoint and the ``/ui`` form both land here so the rules (kill
    switch, link secret, notes, mode, languages, cast) can never drift.

    Args:
        upload: Multipart file part carrying the notes.
        target_language: ISO 639-1 language being taught.
        native_language: ISO 639-1 student language.
        content_length: Raw ``Content-Length`` header, for the early 413.
        mode: Lesson mode: mixed, dual or native.
        collection: Collection whose channels receive the lesson.
        title: Optional lesson title (defaults to the filename stem).
        spec: Optional NarrationSpec JSON.
        voice: Voice of the native-language role (the single role otherwise).
        voice2: Voice of the target-language role; required in 'dual' mode.
        phonetic_adaptation: Adapt the target-language terms the class quotes
            to how a native of ``native_language`` would read them (7.16).
        run_in_modal: For the QWEN3_TTS backend, run synthesis in Modal instead
            of the gateway local (default).

    Returns:
        Dict with workflow_id, collection, title, mode and status_url.

    Raises:
        SubmitError: 403 disabled, 413 too large, 415 unsupported type, 422
            invalid input, 502 Temporal failure, 503 no link secret.
    """
    settings = get_settings()
    if not settings.teacher_enabled:
        raise SubmitError("Lessons are disabled (TEACHER_ENABLED=false)", status_code=403)
    if not settings.doc_tts_link_secret.strip():
        # A lesson longer than the audio limit must ship a signed URL: without
        # a secret the result would be unusable, so refuse it up front.
        raise SubmitError(
            "DOC_TTS_LINK_SECRET is not configured; signed audio links are off",
            status_code=503,
        )
    check_content_length(content_length, settings.teacher_max_file_bytes)

    filename = Path(upload.filename or "").name.strip()
    if not filename:
        raise SubmitError("file part has no filename", status_code=422)
    target = language_code(target_language, "target_language")
    native = language_code(native_language, "native_language")
    try:
        lesson_mode = LessonMode((mode or "mixed").strip().lower())
    except ValueError as exc:
        raise SubmitError(
            f"mode must be one of {[m.value for m in LessonMode]}: {mode!r}",
            status_code=422,
        ) from exc

    data = await read_upload(upload, settings.teacher_max_file_bytes)
    try:
        notes = validate_notes(data, filename)
    except LessonError as exc:
        detail = str(exc)
        if "unsupported file type" in detail:
            code = 415
        elif "too long" in detail:
            code = 413
        else:
            code = 422
        raise SubmitError(detail, status_code=code) from exc

    cast = resolve_cast(
        spec,
        voice,
        target,
        mode=lesson_mode,
        native_language=native,
        voice2=voice2,
        run_in_modal=run_in_modal,
    )
    try:
        validate_mode(lesson_mode, cast)
    except LessonError as exc:
        raise SubmitError(str(exc), status_code=422) from exc
    # Phase 7.16: reject an adaptation the render cannot honor before anything
    # is written or queued (kill switch, languages, engine, cast, heuristics row).
    await validate_phonetic_adaptation(phonetic_adaptation, cast, target, native, mode=lesson_mode)

    from src.api.services.temporal_admin import new_lesson_run_id

    workflow_id = new_lesson_run_id()
    upload_path = lesson_upload_path(workflow_id, filename)
    atomic_write(upload_path, data)
    clean_title = (title or "").strip() or Path(filename).stem
    collection_name = (collection or "").strip() or "default"
    channels, chat_id, email_recipients = collection_delivery(collection_name)
    update_lesson_meta(
        workflow_id,
        {
            "title": clean_title,
            "filename": filename,
            "mode": lesson_mode.value,
            "target_language": target,
            "native_language": native,
            "collection": collection_name,
            "notes_chars": notes.char_count,
            "phonetic_adaptation": phonetic_adaptation,
        },
    )

    repo = AudioJobsRepository()
    await repo.start_job(workflow_id, collection=collection_name, kind=JOB_KIND)
    try:
        from src.api.services.temporal_admin import TemporalAdminService
        from src.workers.lesson_workflows import LessonConfig

        await TemporalAdminService().start_lesson(
            LessonConfig(
                notes_path=str(upload_path),
                filename=filename,
                mode=lesson_mode.value,
                spec=cast.model_dump(mode="json"),
                target_language=target,
                native_language=native,
                title=(title or "").strip(),
                collection=collection_name,
                phonetic_adaptation=phonetic_adaptation,
                delivery_task_queue=settings.temporal_task_queue,
                delivery_channels=channels,
                chat_id=chat_id,
                email_recipients=email_recipients,
            ),
            workflow_id,
        )
    except Exception as exc:
        logger.error("lesson_start_failed", workflow_id=workflow_id, error=str(exc), exc_info=True)
        await repo.finish_job(
            workflow_id,
            status="FINISHED_ERROR",
            error=f"could not start the workflow: {exc}",
            kind=JOB_KIND,
        )
        upload_path.unlink(missing_ok=True)
        raise SubmitError("Could not start the lesson workflow", status_code=502) from exc

    logger.info(
        "lesson_submitted",
        workflow_id=workflow_id,
        filename=filename,
        mode=lesson_mode.value,
        collection=collection_name,
        notes_chars=notes.char_count,
    )
    return {
        "workflow_id": workflow_id,
        "collection": collection_name,
        "title": clean_title,
        "mode": lesson_mode.value,
        "status_url": f"/api/admin/lessons/{workflow_id}",
    }
