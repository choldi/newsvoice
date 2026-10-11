"""Unit tests for the Phase 7.12 language-teacher lesson service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.models.lesson import LessonContent, LessonMode
from src.api.models.narration import NarrationSpec, SpeakerRole
from src.api.services.lesson import (
    LessonError,
    attachment_text,
    build_lesson_messages,
    build_script,
    lesson_audio_link,
    lesson_meta_path,
    lesson_script_path,
    lesson_text_path,
    lesson_upload_path,
    parse_lesson_content,
    read_lesson_meta,
    resolve_cast,
    roles_summary,
    save_lesson,
    should_attach_audio,
    update_lesson_meta,
    validate_lesson_id,
    validate_mode,
    validate_notes,
)
from src.api.services.submit import SubmitError


@pytest.fixture(autouse=True)
def _teacher_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point lesson storage at tmp_path and enable signed links."""
    monkeypatch.setattr("src.core.config.settings.teacher_dir", str(tmp_path / "lessons"))
    monkeypatch.setattr("src.core.config.settings.doc_tts_link_secret", "unit-test-secret")
    return tmp_path / "lessons"


def _spec(*voices: str) -> NarrationSpec:
    """Build a cast with one role per given voice id."""
    return NarrationSpec(
        language="en",
        roles=[
            SpeakerRole(name=f"profe_{index}", voice_id=voice)
            for index, voice in enumerate(voices or ("voice-1",))
        ],
    )


def _content(roles: tuple[str, ...] = ("profe_0",)) -> LessonContent:
    """Build a minimal valid lesson content."""
    return LessonContent(
        title="Leccion de subjuntivo",
        markdown="## Subjuntivo\n\nExplicacion y ejercicios.",
        segments=[
            {"role": role, "text": f"Hola, soy {role} y vamos a practicar."} for role in roles
        ],
    )


# ---------------------------------------------------------------------------
# validate_notes
# ---------------------------------------------------------------------------


def test_validate_notes_accepts_md_and_txt() -> None:
    """Both supported extensions decode to notes."""
    md = validate_notes(b"# Apuntes\n\nContenido.", "apuntes.md")
    txt = validate_notes(b"Contenido.", "apuntes.txt")
    assert md.kind == "md"
    assert md.char_count == len(md.text)
    assert txt.kind == "txt"


def test_validate_notes_rejects_unsupported_types() -> None:
    """A .docx never reaches the LLM."""
    with pytest.raises(LessonError, match="unsupported file type"):
        validate_notes(b"x", "notas.docx")


def test_validate_notes_rejects_empty_and_blank() -> None:
    """Empty bytes and whitespace-only files are refused."""
    with pytest.raises(LessonError, match="empty"):
        validate_notes(b"", "notas.md")
    with pytest.raises(LessonError, match="no text content"):
        validate_notes(b"   \n\n", "notas.md")


def test_validate_notes_enforces_the_char_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notes over teacher_max_notes_chars fail with the numbers in hand."""
    monkeypatch.setattr("src.core.config.settings.teacher_max_notes_chars", 1000)
    with pytest.raises(LessonError, match="notes too long"):
        validate_notes(b"palabra " * 500, "notas.md")


def test_validate_notes_decodes_latin1_fallback() -> None:
    """Invalid UTF-8 still decodes instead of crashing the submit."""
    notes = validate_notes(b"caf\xe9 con leche", "notas.txt")
    assert "café" in notes.text


# ---------------------------------------------------------------------------
# paths and metadata
# ---------------------------------------------------------------------------


def test_lesson_paths_use_the_configured_directory() -> None:
    """Every artifact lives under teacher_dir, keyed by the workflow id."""
    upload = lesson_upload_path("lesson-abc", "notas.md")
    text = lesson_text_path("lesson-abc")
    script = lesson_script_path("lesson-abc")
    meta = lesson_meta_path("lesson-abc")
    assert upload.name == "lesson-abc.md"
    assert text.name == "lesson-abc.md"
    assert script.name == "lesson-abc.script.json"
    assert meta.name == "lesson-abc.json"
    roots = {path.parent.parent for path in (upload, text, script, meta)}
    assert len(roots) == 1, "every artifact must live under teacher_dir"


def test_lesson_paths_reject_foreign_ids() -> None:
    """Ids become file paths: traversal attempts are refused."""
    with pytest.raises(LessonError, match="invalid lesson id"):
        lesson_text_path("../../etc/passwd")
    with pytest.raises(LessonError, match="invalid lesson id"):
        lesson_upload_path("..%2Fx", "a.md")
    assert read_lesson_meta("../../etc/passwd") == {}


def test_lesson_meta_round_trip() -> None:
    """Metadata merges across writes (submit -> generate -> render)."""
    update_lesson_meta("lesson-abc", {"title": "Clase", "mode": "dual"})
    update_lesson_meta("lesson-abc", {"duration_seconds": 120.5})
    meta = read_lesson_meta("lesson-abc")
    assert meta["title"] == "Clase"
    assert meta["mode"] == "dual"
    assert meta["duration_seconds"] == 120.5


def test_validate_lesson_id_accepts_our_ids() -> None:
    """Our own id alphabet passes through unchanged."""
    assert validate_lesson_id("lesson-abc123") == "lesson-abc123"


# ---------------------------------------------------------------------------
# modes and prompts
# ---------------------------------------------------------------------------


def test_dual_mode_needs_two_roles() -> None:
    """'dual' with a one-voice cast is a configuration error, not a surprise."""
    with pytest.raises(LessonError, match="at least two roles"):
        validate_mode(LessonMode.DUAL, _spec("voice-1"))


def test_mixed_and_native_accept_one_role() -> None:
    """Single-voice modes are valid with one role."""
    validate_mode(LessonMode.MIXED, _spec("voice-1"))
    validate_mode(LessonMode.NATIVE, _spec("voice-1"))
    validate_mode(LessonMode.DUAL, _spec("voice-1", "voice-2"))


def test_resolve_cast_dual_builds_the_two_roles() -> None:
    """'dual' without a spec turns the two voice fields into the cast."""
    spec = resolve_cast(
        None,
        "voice-native",
        "en",
        mode="dual",
        native_language="es",
        voice2="voice-target",
    )
    assert [(r.name, r.voice_id, r.language) for r in spec.roles] == [
        ("profe_native", "voice-native", "es"),
        ("profe_target", "voice-target", "en"),
    ]


def test_resolve_cast_dual_needs_both_voices() -> None:
    """A missing second voice is a 422, never a silent one-role class."""
    with pytest.raises(SubmitError) as exc:
        resolve_cast(None, "voice-1", "en", mode="dual", native_language="es")
    assert exc.value.status_code == 422
    assert "voice2" in exc.value.message


def test_resolve_cast_keeps_one_role_outside_dual() -> None:
    """'mixed' and 'native' stay single-voice; voice2 is not part of the cast."""
    for mode in ("mixed", "native"):
        spec = resolve_cast(
            None,
            "voice-1",
            "en",
            mode=mode,
            native_language="es",
            voice2="voice-2",
        )
        assert [r.name for r in spec.roles] == ["teacher"]
        assert spec.roles[0].voice_id == "voice-1"


def test_resolve_cast_prefers_the_spec_json() -> None:
    """A hand-written spec wins over the voice fields, dual included."""
    spec_json = json.dumps(
        {"language": "en", "roles": [{"name": "profe_es", "voice_id": "clone-es"}]}
    )
    spec = resolve_cast(
        spec_json, "voice-1", "en", mode="dual", native_language="es", voice2="voice-2"
    )
    assert [r.name for r in spec.roles] == ["profe_es"]


def test_resolve_cast_defaults_to_gateway_local() -> None:
    """Without the checkbox the render stays on the gateway local (no Modal)."""
    spec = resolve_cast(None, "voice-1", "en")
    assert spec.run_in_modal is False


def test_resolve_cast_carries_run_in_modal() -> None:
    """The lesson form checkbox turns the Modal route on for qwen3_tts."""
    spec = resolve_cast(None, "anchor_neutral_m", "en", run_in_modal=True)
    assert spec.run_in_modal is True
    assert str(spec.tts_backend) == "qwen3_tts"


def test_roles_summary_reports_languages() -> None:
    """The prompt receives names and languages as JSON."""
    spec = _spec("voice-1", "voice-2")
    spec.roles[1].language = "es"
    summary = json.loads(roles_summary(spec))
    assert summary == [
        {"name": "profe_0", "language": "en"},
        {"name": "profe_1", "language": "es"},
    ]


def test_build_lesson_messages_include_mode_and_notes() -> None:
    """The chat carries the mode instructions and the student's notes."""
    messages = build_lesson_messages(
        notes="El subjuntivo se usa para dudas.",
        mode=LessonMode.DUAL,
        spec=_spec("a", "b"),
        target_language="en",
        native_language="es",
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Dual (dos voces)" in messages[0]["content"]
    assert "El subjuntivo se usa para dudas." in messages[1]["content"]
    assert '"en"' in messages[0]["content"]


# ---------------------------------------------------------------------------
# parse / script / attachment
# ---------------------------------------------------------------------------


def test_parse_lesson_content_accepts_clean_json() -> None:
    """A plain JSON object parses into a lesson."""
    payload = json.dumps(
        {"title": "T", "markdown": "# T\n\ntexto", "segments": [{"role": "r", "text": "hola"}]}
    )
    content = parse_lesson_content(payload)
    assert content.title == "T"
    assert len(content.segments) == 1


def test_parse_lesson_content_tolerates_code_fences_and_prose() -> None:
    """Models occasionally decorate an otherwise correct answer."""
    payload = """Claro, aquí tienes la clase:
```json
{"title": "T", "markdown": "# T\\n\\ntexto", "segments": [{"role": "r", "text": "hola"}]}
```
"""
    content = parse_lesson_content(payload)
    assert content.title == "T"


def test_parse_lesson_content_rejects_non_json() -> None:
    """A reply without a JSON object is a permanent failure."""
    with pytest.raises(LessonError, match="no JSON object"):
        parse_lesson_content("No puedo generar la clase ahora.")


def test_parse_lesson_content_rejects_a_bad_schema() -> None:
    """JSON that does not match the lesson schema fails with the reason."""
    with pytest.raises(LessonError, match="invalid"):
        parse_lesson_content(json.dumps({"title": "T", "markdown": "x"}))


def test_build_script_validates_roles_against_the_cast() -> None:
    """Segments referencing an unknown role are rejected."""
    spec = _spec("voice-1")
    content = LessonContent(
        title="T",
        markdown="# T",
        segments=[{"role": "ghost", "text": "hola"}],
    )
    with pytest.raises(LessonError, match="does not match the cast"):
        build_script(content, spec)


def test_build_script_accepts_the_declared_roles() -> None:
    """Segments written with the cast's names become a valid script."""
    spec = _spec("voice-1")
    script = build_script(_content(), spec)
    assert script.source == "llm"
    assert script.language == "en"
    assert len(script.segments) == 1


def test_attachment_text_titles_unheaded_markdown() -> None:
    """The attachment always starts with a title heading."""
    content = LessonContent(
        title="Mi clase", markdown="Texto sin encabezado.", segments=[{"role": "r", "text": "x"}]
    )
    text = attachment_text(content)
    assert text.startswith("# Mi clase\n\n")
    assert text.endswith("\n")


def test_attachment_text_keeps_an_existing_heading() -> None:
    """Markdown that already has a heading is not double-titled."""
    content = LessonContent(
        title="Mi clase", markdown="# Ya titulada", segments=[{"role": "r", "text": "x"}]
    )
    assert attachment_text(content).startswith("# Ya titulada")


# ---------------------------------------------------------------------------
# audio threshold and signed link
# ---------------------------------------------------------------------------


def test_should_attach_audio_honours_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audio at or under the limit attaches; over it becomes a link."""
    monkeypatch.setattr("src.core.config.settings.teacher_max_audio_seconds", 300.0)
    assert should_attach_audio(300.0) is True
    assert should_attach_audio(300.1) is False
    monkeypatch.setattr("src.core.config.settings.teacher_max_audio_seconds", 60.0)
    assert should_attach_audio(60.0) is True


def test_lesson_audio_link_uses_the_lesson_prefix() -> None:
    """Lesson links are served under /api/audio/lesson with the shared secret."""
    link = lesson_audio_link("lesson-abc123")
    assert link.startswith("/api/audio/lesson/lesson-abc123?exp=")


# ---------------------------------------------------------------------------
# save_lesson
# ---------------------------------------------------------------------------


def test_save_lesson_writes_markdown_and_script() -> None:
    """Both artifacts land on disk; the script is the validated dump."""
    spec = _spec("voice-1")
    content = _content()
    script = build_script(content, spec)

    text_path = save_lesson("lesson-abc", content, script)

    assert Path(text_path).is_file()
    stored_text = Path(text_path).read_text(encoding="utf-8")
    # The markdown already carries a heading, so no title is prepended.
    assert stored_text.startswith("## Subjuntivo")
    assert stored_text.endswith("\n")
    stored = json.loads(lesson_script_path("lesson-abc").read_text(encoding="utf-8"))
    assert stored["source"] == "llm"
    assert stored["segments"][0]["role"] == "profe_0"
