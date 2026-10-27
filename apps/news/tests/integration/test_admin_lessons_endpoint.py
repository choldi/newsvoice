"""Integration tests for the Phase 7.12 lesson endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import AudioJobsRepository
from src.api.main import app
from src.api.services.signed_links import LESSON_AUDIO_LINK_PREFIX, build_signed_path
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)
anon_client = TestClient(app)

JOB_KIND = "lesson"
NOTES = b"# Apuntes de clase\n\nEl subjuntivo se usa para dudas.\n"


@pytest.fixture(autouse=True)
def _lesson_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate lesson storage, enable auth and signed links for every test."""
    monkeypatch.setattr(settings, "teacher_dir", str(tmp_path / "lessons"))
    monkeypatch.setattr(settings, "narration_cache_dir", str(tmp_path / "narration"))
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    monkeypatch.setattr(settings, "doc_tts_link_secret", "unit-test-secret")
    monkeypatch.setattr(settings, "teacher_enabled", True)
    return tmp_path / "lessons"


def _write_collection(path: Path, **extra: str) -> None:
    """Write one collection named 'tech' with email + telegram channels."""
    lines = [
        "[[collections]]",
        'name = "tech"',
        'time = "08:00"',
        'feed_urls = ["https://example.com/rss"]',
        'delivery_channels = ["telegram", "email"]',
        'email_recipients = ["me@example.com"]',
        'chat_id = "4242"',
    ]
    lines.extend(f"{key} = {value}" for key, value in extra.items())
    path.write_text("\n".join(lines) + "\n")


def _submit(
    *,
    client_: TestClient | None = None,
    filename: str = "apuntes.md",
    content: bytes = NOTES,
    **form: str,
) -> object:
    """POST notes to the lesson endpoint."""
    target = client_ or client
    return target.post(
        "/api/admin/lessons",
        files={"file": (filename, content, "text/markdown")},
        data=form or None,
    )


def _job(workflow_id: str) -> dict:
    """Fetch a lesson job row."""
    job = asyncio.run(AudioJobsRepository().get_job(workflow_id, kind=JOB_KIND))
    assert job is not None, f"job {workflow_id} not found"
    return job


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submit_requires_the_admin_token(_lesson_env: Path) -> None:
    """Without the token the endpoint answers 401."""
    response = _submit(client_=anon_client, target_language="en", native_language="es")
    assert response.status_code == 401


def test_submit_starts_the_workflow_with_channels_and_cast(
    _lesson_env: Path,
) -> None:
    """A valid upload answers 202, stores the notes and opens the job row."""
    _write_collection(_lesson_env.parent / "collections.toml")
    spec = json.dumps(
        {
            "language": "en",
            "roles": [
                {"name": "profe_es", "voice_id": "clone-es"},
                {"name": "profe_en", "voice_id": "clone-en"},
            ],
        }
    )
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
    ) as start:
        response = _submit(
            target_language="en",
            native_language="es",
            mode="dual",
            collection="tech",
            title="Subjuntivo",
            spec=spec,
        )

    assert response.status_code == 202
    body = response.json()
    assert body["workflow_id"].startswith("lesson-")
    assert body["mode"] == "dual"
    assert body["title"] == "Subjuntivo"
    assert body["collection"] == "tech"
    assert body["status_url"] == f"/api/admin/lessons/{body['workflow_id']}"

    config = start.call_args.args[0]
    assert start.call_args.args[1] == body["workflow_id"]
    assert config.mode == "dual"
    assert [role["name"] for role in config.spec["roles"]] == ["profe_es", "profe_en"]
    assert config.target_language == "en"
    assert config.native_language == "es"
    assert config.delivery_channels == ["telegram", "email"]
    assert config.email_recipients == ["me@example.com"]
    assert config.chat_id == "4242"
    assert config.delivery_task_queue == settings.temporal_task_queue
    assert Path(config.notes_path).is_file()

    job = _job(body["workflow_id"])
    assert job["status"] == "STARTING"
    assert job["collection"] == "tech"
    meta = json.loads((_lesson_env / "meta" / f"{body['workflow_id']}.json").read_text())
    assert meta["mode"] == "dual"
    assert meta["notes_chars"] == len(NOTES.decode().strip())


def test_submit_builds_a_default_cast_from_voice(_lesson_env: Path) -> None:
    """Without a spec, the single voice field becomes a one-role cast."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
    ) as start:
        response = _submit(target_language="en", native_language="es", voice="es-MX-JorgeNeural")

    assert response.status_code == 202
    config = start.call_args.args[0]
    assert len(config.spec["roles"]) == 1
    assert config.spec["roles"][0]["voice_id"] == "es-MX-JorgeNeural"


def test_submit_rejects_unsupported_notes(_lesson_env: Path) -> None:
    """A .pdf of notes never reaches the LLM."""
    response = _submit(filename="clase.pdf", target_language="en", native_language="es")
    assert response.status_code == 415


def test_submit_rejects_an_empty_file(_lesson_env: Path) -> None:
    """Empty notes are a 422."""
    response = _submit(content=b"", target_language="en", native_language="es")
    assert response.status_code == 422


def test_submit_rejects_oversized_notes(_lesson_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files beyond teacher_max_file_bytes are refused with 413."""
    monkeypatch.setattr(settings, "teacher_max_file_bytes", 1024)
    response = _submit(content=b"x" * 2048, target_language="en", native_language="es")
    assert response.status_code == 413


def test_submit_rejects_bad_languages_and_modes(_lesson_env: Path) -> None:
    """ISO 639-1 codes and known modes are validated before anything starts."""
    assert _submit(target_language="english", native_language="es").status_code == 422
    assert _submit(target_language="en", native_language="Esp").status_code == 422
    assert _submit(target_language="en", native_language="es", mode="karate").status_code == 422


def test_submit_rejects_dual_without_two_voices(_lesson_env: Path) -> None:
    """'dual' with a single voice is refused instead of downgrading to one role."""
    response = _submit(target_language="en", native_language="es", mode="dual")
    assert response.status_code == 422
    assert "voice2" in response.json()["detail"]


def test_submit_builds_the_dual_cast_from_two_voices(_lesson_env: Path) -> None:
    """The two voice fields become the native/target roles of a 'dual' class."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
    ) as start:
        response = _submit(
            target_language="en",
            native_language="es",
            mode="dual",
            voice="clone-native",
            voice2="clone-target",
        )

    assert response.status_code == 202
    roles = start.call_args.args[0].spec["roles"]
    assert [role["name"] for role in roles] == ["profe_native", "profe_target"]
    assert [role["voice_id"] for role in roles] == ["clone-native", "clone-target"]
    assert [role["language"] for role in roles] == ["es", "en"]


def test_submit_rejects_invalid_spec_json(_lesson_env: Path) -> None:
    """Malformed spec JSON answers 422 with the reason."""
    response = _submit(
        target_language="en", native_language="es", spec='{"roles": [{"name": "x"}]}'
    )
    assert response.status_code == 422


def test_submit_is_disabled_by_the_kill_switch(
    _lesson_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEACHER_ENABLED=false closes the endpoint."""
    monkeypatch.setattr(settings, "teacher_enabled", False)
    response = _submit(target_language="en", native_language="es")
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_submit_requires_a_link_secret(_lesson_env: Path, monkeypatch) -> None:
    """Without the shared secret a long lesson would be undeliverable: fail fast."""
    monkeypatch.setattr(settings, "doc_tts_link_secret", "")
    response = _submit(target_language="en", native_language="es")
    assert response.status_code == 503
    assert "DOC_TTS_LINK_SECRET" in response.json()["detail"]


def test_submit_temporal_failure_is_502_and_marks_the_job(_lesson_env: Path) -> None:
    """A Temporal outage leaves an explained job row and no orphan upload."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
        side_effect=RuntimeError("temporal unreachable"),
    ):
        response = _submit(target_language="en", native_language="es")

    assert response.status_code == 502
    assert list((_lesson_env / "uploads").glob("*")) == []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_unknown_lesson_is_404(_lesson_env: Path) -> None:
    """Foreign or missing ids answer 404."""
    assert client.get("/api/admin/lessons/lesson-nope").status_code == 404
    assert client.get("/api/admin/lessons/not-a-lesson").status_code == 404


def test_status_reports_generation_progress(_lesson_env: Path) -> None:
    """A running lesson answers GENERATING without an audio link."""
    from src.api.services.lesson import update_lesson_meta

    asyncio.run(AudioJobsRepository().start_job("lesson-run1", collection="tech", kind=JOB_KIND))
    asyncio.run(AudioJobsRepository().set_generating("lesson-run1", kind=JOB_KIND))
    update_lesson_meta("lesson-run1", {"title": "Clase", "mode": "mixed"})

    body = client.get("/api/admin/lessons/lesson-run1").json()

    assert body["job_status"] == "GENERATING"
    assert body["title"] == "Clase"
    assert body["mode"] == "mixed"
    assert body["audio_url"] is None
    assert body["respelling"] is None, "no adaptation metadata unless the class asked for it"


def test_status_returns_a_signed_link_when_finished(_lesson_env: Path) -> None:
    """A finished lesson answers FINISHED_OK with its signed URL."""
    from src.api.services.audio_files import atomic_write
    from src.api.services.lesson import update_lesson_meta

    audio_dir = Path(settings.narration_cache_dir)
    audio_path = audio_dir / "lesson_done.m4a"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(audio_path, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)

    repo = AudioJobsRepository()
    asyncio.run(repo.start_job("lesson-done", collection="tech", kind=JOB_KIND))
    asyncio.run(
        repo.finish_job(
            "lesson-done",
            status="FINISHED_OK",
            audio_path=str(audio_path),
            elapsed_seconds=12.5,
            kind=JOB_KIND,
        )
    )
    update_lesson_meta(
        "lesson-done",
        {"title": "Clase", "mode": "dual", "duration_seconds": 180.0, "char_count": 2400},
    )

    body = client.get("/api/admin/lessons/lesson-done").json()

    assert body["job_status"] == "FINISHED_OK"
    assert body["audio_url"].startswith(f"{LESSON_AUDIO_LINK_PREFIX}/lesson-done?exp=")
    assert body["audio_url"] == build_signed_path(
        "lesson-done", prefix=LESSON_AUDIO_LINK_PREFIX, ttl=settings.teacher_link_ttl_seconds
    )
    assert body["duration_estimate"] == 180.0
    assert body["char_count"] == 2400


def test_status_reports_a_failure_with_its_reason(_lesson_env: Path) -> None:
    """FINISHED_ERROR carries the reason the worker recorded."""
    repo = AudioJobsRepository()
    asyncio.run(repo.start_job("lesson-bad", collection="tech", kind=JOB_KIND))
    asyncio.run(
        repo.finish_job(
            "lesson-bad",
            status="FINISHED_ERROR",
            error="LLM reply is not valid JSON",
            kind=JOB_KIND,
        )
    )

    body = client.get("/api/admin/lessons/lesson-bad").json()

    assert body["job_status"] == "FINISHED_ERROR"
    assert "not valid JSON" in body["error"]
    assert body["audio_url"] is None


# ---------------------------------------------------------------------------
# Signed lesson audio link
# ---------------------------------------------------------------------------


def _finished_lesson(workflow_id: str) -> Path:
    """Create a finished lesson job plus its audio file; return the path."""
    from src.api.services.audio_files import atomic_write

    audio_path = Path(settings.narration_cache_dir) / f"{workflow_id}.m4a"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(audio_path, b"ID3" + b"\x00" * 32)
    repo = AudioJobsRepository()
    asyncio.run(repo.start_job(workflow_id, collection="tech", kind=JOB_KIND))
    asyncio.run(
        repo.finish_job(
            workflow_id, status="FINISHED_OK", audio_path=str(audio_path), kind=JOB_KIND
        )
    )
    return audio_path


def test_lesson_audio_link_serves_the_file_without_the_admin_token(
    _lesson_env: Path,
) -> None:
    """The signed link works from a browser with no admin credentials."""
    _finished_lesson("lesson-link1")
    path = build_signed_path(
        "lesson-link1", prefix=LESSON_AUDIO_LINK_PREFIX, ttl=settings.teacher_link_ttl_seconds
    )

    response = anon_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")


def test_lesson_audio_link_rejects_a_forged_signature(_lesson_env: Path) -> None:
    """A flipped signature answers 403, not the audio."""
    _finished_lesson("lesson-link2")
    path = build_signed_path(
        "lesson-link2", prefix=LESSON_AUDIO_LINK_PREFIX, ttl=settings.teacher_link_ttl_seconds
    )
    forged = path[:-2] + ("00" if not path.endswith("00") else "11")

    assert anon_client.get(forged).status_code == 403


def test_lesson_audio_link_requires_query_parameters(_lesson_env: Path) -> None:
    """A bare path (no exp/sig) is a validation error, not an open door."""
    _finished_lesson("lesson-link3")
    assert anon_client.get(f"{LESSON_AUDIO_LINK_PREFIX}/lesson-link3").status_code == 422


def test_lesson_audio_link_pending_lesson_is_404(_lesson_env: Path) -> None:
    """Signing a lesson that has not finished does not unlock anything."""
    asyncio.run(AudioJobsRepository().start_job("lesson-pending", collection="tech", kind=JOB_KIND))
    path = build_signed_path(
        "lesson-pending", prefix=LESSON_AUDIO_LINK_PREFIX, ttl=settings.teacher_link_ttl_seconds
    )
    assert anon_client.get(path).status_code == 404


def test_lesson_audio_link_rejects_a_foreign_path(_lesson_env: Path) -> None:
    """A row pointing outside the narration cache cannot serve a foreign file."""
    from src.api.services.audio_files import atomic_write

    outside = _lesson_env.parent / "secrets.txt"
    atomic_write(outside, b"not audio")
    repo = AudioJobsRepository()
    asyncio.run(repo.start_job("lesson-evil", collection="tech", kind=JOB_KIND))
    asyncio.run(
        repo.finish_job("lesson-evil", status="FINISHED_OK", audio_path=str(outside), kind=JOB_KIND)
    )
    path = build_signed_path(
        "lesson-evil", prefix=LESSON_AUDIO_LINK_PREFIX, ttl=settings.teacher_link_ttl_seconds
    )

    assert anon_client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# Phonetic adaptation (Phase 7.16)
# ---------------------------------------------------------------------------

QWEN_SPEC = json.dumps(
    {
        "language": "en",
        "tts_backend": "qwen3_tts",
        "roles": [
            {"name": "profe_es", "voice_id": "clone-es", "language": "es"},
            {"name": "profe_en", "voice_id": "clone-en", "language": "en"},
        ],
    }
)


def test_submit_adapts_when_requested(_lesson_env: Path) -> None:
    """The flag travels to the workflow config and into the lesson metadata."""
    from src.api.services.lesson import read_lesson_meta

    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
    ) as start:
        response = _submit(
            target_language="en",
            native_language="es",
            mode="dual",
            spec=QWEN_SPEC,
            phonetic_adaptation="on",
        )

    assert response.status_code == 202
    config = start.call_args[0][0]
    assert config.phonetic_adaptation is True
    meta = read_lesson_meta(response.json()["workflow_id"])
    assert meta["phonetic_adaptation"] is True


def test_submit_without_adaptation_defaults_to_off(_lesson_env: Path) -> None:
    """A plain submit keeps the flag false so the workflow skips the step."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_lesson",
        new_callable=AsyncMock,
    ) as start:
        response = _submit(target_language="en", native_language="es")

    assert response.status_code == 202
    assert start.call_args[0][0].phonetic_adaptation is False


def test_submit_adaptation_requires_the_qwen3_backend(_lesson_env: Path) -> None:
    """A gateway cast cannot consume tts_text: 422 before anything queues."""
    plain_spec = json.dumps(
        {
            "language": "en",
            "roles": [
                {"name": "profe_es", "voice_id": "clone-es", "language": "es"},
                {"name": "profe_en", "voice_id": "clone-en", "language": "en"},
            ],
        }
    )

    response = _submit(
        target_language="en",
        native_language="es",
        mode="dual",
        spec=plain_spec,
        phonetic_adaptation="on",
    )

    assert response.status_code == 422
    assert "qwen3_tts" in response.json()["detail"]


def test_submit_adaptation_rejects_equal_languages(_lesson_env: Path) -> None:
    """Adapting a language to itself is refused before the cast is resolved."""
    response = _submit(
        target_language="es", native_language="es", mode="mixed", phonetic_adaptation="on"
    )

    assert response.status_code == 422
    assert "different target and native" in response.json()["detail"]


def test_submit_adaptation_reports_an_unconfigured_pair(_lesson_env: Path) -> None:
    """No heuristics row for the pair: the error names the GUI to create it."""
    response = _submit(
        target_language="de",
        native_language="es",
        mode="dual",
        spec=QWEN_SPEC,
        phonetic_adaptation="on",
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "no phonetic heuristics for de -> es" in detail
    assert "/ui/phonetics" in detail


def test_status_reports_the_respelling_metadata(_lesson_env: Path) -> None:
    """The stored {term: respelling} map is readable from the status endpoint."""
    from src.api.services.lesson import update_lesson_meta

    asyncio.run(AudioJobsRepository().start_job("lesson-adapt", collection="tech", kind=JOB_KIND))
    asyncio.run(AudioJobsRepository().set_generating("lesson-adapt", kind=JOB_KIND))
    update_lesson_meta(
        "lesson-adapt",
        {
            "title": "Saludos",
            "mode": "dual",
            "respelling": {
                "language_origin": "en",
                "language_target": "es",
                "terms": {"startup": "estartap"},
                "segments": [
                    {
                        "role": "profe_native",
                        "segment_index": 0,
                        "original_text": "decimos startup aqui",
                        "respelled_text": "decimos estartap aqui",
                        "term_map": {"startup": "estartap"},
                    }
                ],
            },
        },
    )

    body = client.get("/api/admin/lessons/lesson-adapt").json()

    assert body["respelling"]["terms"] == {"startup": "estartap"}
    assert body["respelling"]["language_origin"] == "en"
    assert body["respelling"]["segments"][0]["role"] == "profe_native"
    assert body["respelling"]["segments"][0]["term_map"] == {"startup": "estartap"}
