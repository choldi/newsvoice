"""Integration tests for the tools page (Phase 7.10-7.12 GUI)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import RunsRepository
from src.api.main import app
from src.api.services.submit import SubmitError
from src.core.config import settings

TOKEN = "secret-admin-token"


@pytest.fixture(autouse=True)
def _ui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate collections, storage and auth; keep the gateway out of tests."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr(settings, "teacher_dir", str(tmp_path / "lessons"))
    monkeypatch.setattr(settings, "narration_cache_dir", str(tmp_path / "narration"))
    monkeypatch.setattr(settings, "audiobook_dir", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "doc_tts_link_secret", "unit-test-secret")
    monkeypatch.setattr(settings, "teacher_enabled", True)
    monkeypatch.setattr(settings, "doc_tts_enabled", True)
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["en", "es"]))
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(return_value=[{"voice_id": "es-mx-male", "language": "es"}]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(return_value=[{"voice_id": "clone-1", "language": "es"}]),
    )
    return tmp_path / "collections.toml"


def _client() -> TestClient:
    """Build a client and authenticate it through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _write_collection(path: Path, *, narration: bool = False) -> None:
    """Write a 'tech' collection, optionally with a NarrationSpec."""
    lines = [
        "[[collections]]",
        'name = "tech"',
        'time = "08:00"',
        'feed_urls = ["https://example.com/rss"]',
        'language = "en"',
        'delivery_channels = ["telegram"]',
    ]
    if narration:
        lines += [
            "narration_enabled = true",
            'narration_strategy = "turns"',
            "[collections.narration_spec]",
            'language = "en"',
            "[[collections.narration_spec.roles]]",
            'name = "anchor"',
            'voice_id = "es-mx-male"',
        ]
    path.write_text("\n".join(lines) + "\n")


def _flash_of(location: str) -> str:
    """Decode the flash message carried by a redirect location."""
    from urllib.parse import unquote

    return unquote(location)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_tools_requires_login() -> None:
    """Without a cookie the tools page redirects to the login."""
    client = TestClient(app)
    response = client.get("/ui/tools", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/login"


def test_tools_page_renders_the_four_tabs() -> None:
    """The page shows every implemented tool and its polling container."""
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    for needle in (
        "Resumen de URL",
        "TTS de documentos",
        "Clase de idiomas",
        "Podcast de una colección",
        'id="jobs-url"',
        'id="jobs-doc_tts"',
        'id="jobs-lesson"',
        'id="jobs-narration"',
        'action="/ui/tools/url"',
        'action="/ui/tools/doc-tts"',
        'action="/ui/tools/lessons"',
        'action="/ui/tools/podcast"',
    ):
        assert needle in response.text, needle
    assert '<a class="nav active" href="/ui/tools">Herramientas</a>' in response.text


# ---------------------------------------------------------------------------
# URL summary
# ---------------------------------------------------------------------------


def test_url_submit_requires_a_url() -> None:
    """An empty URL flashes an error and starts nothing."""
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
    ) as start:
        response = client.post("/ui/tools/url", data={}, follow_redirects=False)

    assert response.status_code == 302
    assert "URL es obligatoria" in _flash_of(response.headers["location"])
    start.assert_not_called()


def test_url_submit_starts_the_workflow(_ui_env: Path) -> None:
    """A valid URL forwards collection and flags to the shared starter."""
    _write_collection(_ui_env)
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
        return_value="url-summary-tech-1",
    ) as start:
        response = client.post(
            "/ui/tools/url",
            data={"url": "https://example.com/a", "collection": "tech", "store": "on"},
            follow_redirects=False,
        )

    assert "url-summary-tech-1" in _flash_of(response.headers["location"])
    args, kwargs = start.call_args
    assert args[0].name == "tech"
    assert kwargs["url"] == "https://example.com/a"
    assert kwargs["store"] is True
    assert kwargs["deliver"] is False, "an unchecked box must not deliver"


def test_url_submit_unknown_collection_fails() -> None:
    """An unknown (non-default) collection is rejected before Temporal."""
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_url_summarize",
        new_callable=AsyncMock,
    ) as start:
        response = client.post(
            "/ui/tools/url",
            data={"url": "https://example.com/a", "collection": "nope"},
            follow_redirects=False,
        )

    assert "no encontrada" in _flash_of(response.headers["location"])
    start.assert_not_called()


# ---------------------------------------------------------------------------
# Documents and lessons (shared submit services)
# ---------------------------------------------------------------------------


def test_doc_submit_calls_the_shared_service() -> None:
    """The form delegates to submit_document with the typed fields."""
    client = _client()
    result = {
        "workflow_id": "doc-tts-abc",
        "title": "Mi libro",
        "filename": "libro.pdf",
        "kind": "pdf",
        "status_url": "/api/admin/documents/tts/doc-tts-abc",
    }
    with patch(
        "src.api.routes_ui.submit_document",
        new_callable=AsyncMock,
        return_value=result,
    ) as submit:
        response = client.post(
            "/ui/tools/doc-tts",
            files={"file": ("libro.pdf", b"%PDF-fake", "application/pdf")},
            data={"title": "Mi libro", "voice": "es-MX-JorgeNeural"},
            follow_redirects=False,
        )

    assert "doc-tts-abc" in _flash_of(response.headers["location"])
    kwargs = submit.call_args.kwargs
    assert kwargs["title"] == "Mi libro"
    assert kwargs["voice"] == "es-MX-JorgeNeural"
    assert kwargs["upload"].filename == "libro.pdf"
    assert kwargs["content_length"], "the size guard must receive the header"


def test_doc_submit_maps_submit_errors_to_flashes() -> None:
    """A SubmitError (413/415/503...) is flashed as-is, not as a 500."""
    client = _client()
    with patch(
        "src.api.routes_ui.submit_document",
        new_callable=AsyncMock,
        side_effect=SubmitError("file larger than 1024 bytes", status_code=413),
    ):
        response = client.post(
            "/ui/tools/doc-tts",
            files={"file": ("libro.pdf", b"x", "application/pdf")},
            follow_redirects=False,
        )

    assert "file larger than 1024 bytes" in _flash_of(response.headers["location"])


def test_lesson_submit_requires_the_notes_file() -> None:
    """A multipart body without the file part flashes an error."""
    client = _client()
    response = client.post(
        "/ui/tools/lessons",
        data={"target_language": "en", "native_language": "es"},
        follow_redirects=False,
    )
    assert "Falta el fichero" in _flash_of(response.headers["location"])


def test_lesson_submit_calls_the_shared_service() -> None:
    """The lesson form forwards mode, languages and destination collection."""
    client = _client()
    result = {
        "workflow_id": "lesson-abc",
        "collection": "tech",
        "title": "Subjuntivo",
        "mode": "dual",
        "status_url": "/api/admin/lessons/lesson-abc",
    }
    with patch(
        "src.api.routes_ui.submit_lesson",
        new_callable=AsyncMock,
        return_value=result,
    ) as submit:
        response = client.post(
            "/ui/tools/lessons",
            files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
            data={
                "mode": "dual",
                "target_language": "en",
                "native_language": "es",
                "collection": "tech",
                "title": "Subjuntivo",
            },
            follow_redirects=False,
        )

    assert "lesson-abc" in _flash_of(response.headers["location"])
    kwargs = submit.call_args.kwargs
    assert kwargs["mode"] == "dual"
    assert kwargs["target_language"] == "en"
    assert kwargs["native_language"] == "es"
    assert kwargs["collection"] == "tech"


def test_lesson_submit_forwards_both_voices() -> None:
    """The dual-mode form hands the native and the target voice to the service."""
    client = _client()
    with patch(
        "src.api.routes_ui.submit_lesson",
        new_callable=AsyncMock,
        return_value={
            "workflow_id": "lesson-def",
            "collection": "default",
            "title": "Dual",
            "mode": "dual",
            "status_url": "/api/admin/lessons/lesson-def",
        },
    ) as submit:
        response = client.post(
            "/ui/tools/lessons",
            files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
            data={
                "mode": "dual",
                "target_language": "en",
                "native_language": "es",
                "voice": "clone-native",
                "voice2": "clone-target",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    kwargs = submit.call_args.kwargs
    assert kwargs["voice"] == "clone-native"
    assert kwargs["voice2"] == "clone-target"


def test_lesson_submit_forwards_the_modal_switch() -> None:
    """The 'Ejecutar en Modal' checkbox reaches the shared submit service."""
    client = _client()
    with patch(
        "src.api.routes_ui.submit_lesson",
        new_callable=AsyncMock,
        return_value={
            "workflow_id": "lesson-ghi",
            "collection": "default",
            "title": "Modal",
            "mode": "mixed",
            "status_url": "/api/admin/lessons/lesson-ghi",
        },
    ) as submit:
        response = client.post(
            "/ui/tools/lessons",
            files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
            data={
                "mode": "mixed",
                "target_language": "en",
                "native_language": "es",
                "narr_run_in_modal": "on",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert submit.call_args.kwargs["run_in_modal"] is True


def test_lesson_form_exposes_the_modal_switch() -> None:
    """The lesson form offers the local/Modal switch, off by default."""
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    lesson_form = response.text.split('action="/ui/tools/lessons"', 1)[1].split("</form>", 1)[0]
    assert 'name="narr_run_in_modal"' in lesson_form
    assert "checked" not in lesson_form.split('name="narr_run_in_modal"', 1)[1][:40]


def test_lesson_form_wires_the_voice_fragment() -> None:
    """The lesson form renders one voice field and swaps it when the mode changes."""
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    assert 'id="lesson-voices"' in response.text
    assert 'hx-get="/ui/fragments/lesson_voices"' in response.text
    assert 'name="voice2"' not in response.text, "mixed is single-voice"


def test_lesson_voice_fragment_follows_the_mode() -> None:
    """The fragment asks for two voices in dual and one in the other modes."""
    client = _client()

    mixed = client.get("/ui/fragments/lesson_voices", params={"mode": "mixed"})
    assert mixed.status_code == 200
    assert 'name="voice"' in mixed.text
    assert 'name="voice2"' not in mixed.text

    dual = client.get(
        "/ui/fragments/lesson_voices",
        params={"mode": "dual", "voice": "es-mx-male", "voice2": "clone-1"},
    )
    assert dual.status_code == 200
    assert 'name="voice"' in dual.text
    assert 'name="voice2"' in dual.text
    assert 'value="es-mx-male" checked' in dual.text, "the pick survives the switch"
    assert 'value="clone-1" checked' in dual.text


# ---------------------------------------------------------------------------
# Phase 7.16: phonetic adaptation switch
# ---------------------------------------------------------------------------


def test_lesson_form_exposes_the_phonetic_adaptation_switch() -> None:
    """The lesson form offers the adaptation, off by default."""
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    lesson_form = response.text.split('action="/ui/tools/lessons"', 1)[1].split("</form>", 1)[0]
    assert 'name="phonetic_adaptation"' in lesson_form
    tag = lesson_form.split('name="phonetic_adaptation"', 1)[1][:120]
    assert "checked" not in tag, "off by default: not every cast is qwen3_tts"


def test_lesson_form_hides_the_switch_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEACHER_RESPELLING_ENABLED=false removes the option entirely."""
    monkeypatch.setattr(settings, "teacher_respelling_enabled", False)
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    assert 'name="phonetic_adaptation"' not in response.text


def test_lesson_submit_forwards_the_phonetic_switch() -> None:
    """A ticked checkbox reaches the shared service as True; absent as False."""
    client = _client()
    result = {
        "workflow_id": "lesson-phon",
        "collection": "default",
        "title": "Fonetica",
        "mode": "mixed",
        "status_url": "/api/admin/lessons/lesson-phon",
    }
    with patch(
        "src.api.routes_ui.submit_lesson", new_callable=AsyncMock, return_value=result
    ) as submit:
        client.post(
            "/ui/tools/lessons",
            files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
            data={
                "target_language": "en",
                "native_language": "es",
                "phonetic_adaptation": "on",
            },
            follow_redirects=False,
        )
        assert submit.call_args.kwargs["phonetic_adaptation"] is True

        client.post(
            "/ui/tools/lessons",
            files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
            data={"target_language": "en", "native_language": "es"},
            follow_redirects=False,
        )
        assert submit.call_args.kwargs["phonetic_adaptation"] is False


# ---------------------------------------------------------------------------
# Podcast
# ---------------------------------------------------------------------------


def test_podcast_needs_a_narration_spec(_ui_env: Path) -> None:
    """A collection without a NarrationSpec explains what is missing."""
    _write_collection(_ui_env, narration=False)
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_narration_render",
        new_callable=AsyncMock,
    ) as start:
        response = client.post(
            "/ui/tools/podcast", data={"collection": "tech"}, follow_redirects=False
        )

    assert "NarrationSpec" in _flash_of(response.headers["location"])
    start.assert_not_called()


def test_podcast_needs_a_previous_run(_ui_env: Path) -> None:
    """With a spec but no runs, the render cannot source its script."""
    _write_collection(_ui_env, narration=True)
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_narration_render",
        new_callable=AsyncMock,
    ) as start:
        response = client.post(
            "/ui/tools/podcast", data={"collection": "tech"}, follow_redirects=False
        )

    assert "genera un digesto" in _flash_of(response.headers["location"])
    start.assert_not_called()


async def test_podcast_renders_from_the_latest_digest(_ui_env: Path) -> None:
    """The render attaches to the collection's digest run and its strategy."""
    _write_collection(_ui_env, narration=True)
    await RunsRepository().record_run(
        workflow_id="digest-pipeline-tech-20261003120000",
        collection="tech",
        status="ok",
        article_count=2,
        cluster_count=1,
    )
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_narration_render",
        new_callable=AsyncMock,
        return_value={"workflow_id": "narration-tech-1", "collection": "tech", "status": "started"},
    ) as start:
        response = client.post(
            "/ui/tools/podcast", data={"collection": "tech"}, follow_redirects=False
        )

    flash = _flash_of(response.headers["location"])
    assert "narration-tech-1" in flash
    assert "digest-pipeline-tech-20261003120000" in flash
    kwargs = start.call_args.kwargs
    assert kwargs["collection"] == "tech"
    assert kwargs["run_id"] == "digest-pipeline-tech-20261003120000"
    assert kwargs["strategy"] == "turns"
    assert kwargs["spec"].roles[0].voice_id == "es-mx-male"


# ---------------------------------------------------------------------------
# Job fragments
# ---------------------------------------------------------------------------


def test_job_fragment_renders_empty_states() -> None:
    """Every kind answers with its own empty state instead of an error."""
    client = _client()
    doc = client.get("/ui/fragments/tool_jobs?kind=doc_tts")
    assert doc.status_code == 200
    assert "Sin trabajos" in doc.text

    url = client.get("/ui/fragments/tool_jobs?kind=url")
    assert "Sin resúmenes" in url.text

    bad = client.get("/ui/fragments/tool_jobs?kind=nope")
    assert "tipo desconocido" in bad.text


def test_job_fragment_lists_url_runs(_ui_env: Path) -> None:
    """URL summary runs recorded in digest_runs appear under kind=url."""
    _write_collection(_ui_env)
    client = _client()

    async def _record() -> None:
        await RunsRepository().record_run(
            workflow_id="url-summary-tech-20261003120000-abcd",
            collection="tech",
            status="ok",
            article_count=1,
            cluster_count=1,
            digest_preview="# Resumen",
        )

    import asyncio

    asyncio.run(_record())

    response = client.get("/ui/fragments/tool_jobs?kind=url")
    assert "url-summary-tech-20261003120000-abcd" in response.text
    assert "Resumen" in response.text


def test_job_fragment_shows_the_signed_lesson_link(_ui_env: Path) -> None:
    """A finished lesson exposes its HMAC link and metadata in the table."""
    import asyncio

    from src.api.db.database import AudioJobsRepository
    from src.api.services.lesson import update_lesson_meta

    async def _job() -> None:
        await AudioJobsRepository().start_job("lesson-done1", collection="tech", kind="lesson")
        await AudioJobsRepository().finish_job(
            "lesson-done1",
            status="FINISHED_OK",
            audio_path=str(Path(settings.narration_cache_dir) / "clase.m4a"),
            kind="lesson",
        )

    audio = Path(settings.narration_cache_dir)
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "clase.m4a").write_bytes(b"ID3" + b"\x00" * 8)
    asyncio.run(_job())
    update_lesson_meta("lesson-done1", {"title": "Subjuntivo", "mode": "dual"})

    client = _client()
    response = client.get("/ui/fragments/tool_jobs?kind=lesson")

    assert "Subjuntivo" in response.text
    assert "/api/audio/lesson/lesson-done1?exp=" in response.text
    assert "dual" in response.text


def test_job_fragment_marks_failed_jobs(_ui_env: Path) -> None:
    """FINISHED_ERROR jobs show their reason and no link."""
    import asyncio

    from src.api.db.database import AudioJobsRepository

    async def _job() -> None:
        await AudioJobsRepository().start_job("doc-bad", collection="documents", kind="doc_tts")
        await AudioJobsRepository().finish_job(
            "doc-bad",
            status="FINISHED_ERROR",
            error="unreadable PDF",
            kind="doc_tts",
        )

    asyncio.run(_job())

    client = _client()
    response = client.get("/ui/fragments/tool_jobs?kind=doc_tts")

    assert "error" in response.text
    assert "unreadable PDF" in response.text
    assert "Descargar" not in response.text


# ---------------------------------------------------------------------------
# Phase 9.8
# ---------------------------------------------------------------------------


def test_tools_doc_form_wires_the_language_select_before_the_picker() -> None:
    """The doc form has one idiom select that filters the edge picker."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    doc_form = page.text.split('action="/ui/tools/doc-tts"', 1)[1].split("</form>", 1)[0]
    assert 'name="language"' in doc_form
    assert 'id="doc-language"' in doc_form
    assert 'hx-target="#voice-results-doc"' in doc_form
    assert 'name="language_q"' not in doc_form, "no internal picker language select"
    assert doc_form.index('name="language"') < doc_form.index('id="picker-doc"'), (
        "the language select precedes the voice picker"
    )


def test_lesson_language_selects_refresh_the_voice_block() -> None:
    """The lesson native/target selects re-fetch #lesson-voices on change."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    lesson_form = page.text.split('action="/ui/tools/lessons"', 1)[1].split("</form>", 1)[0]
    assert 'name="target_language"' in lesson_form
    assert 'name="native_language"' in lesson_form
    assert lesson_form.count('hx-get="/ui/fragments/lesson_voices"') >= 3, (
        "mode + both language selects re-render the voice block"
    )


def test_tools_page_embeds_the_cast_preview_spec() -> None:
    """The cast avanzado box starts with the JSON the current cast would make."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    assert 'id="spec-preview"' in page.text
    assert "<pre>" in page.text
    assert "Selecciona una voz para previsualizar el JSON" in page.text


def test_delete_job_removes_files_row_and_narration_link(_ui_env: Path) -> None:
    """POST delete purges the audio, sidecar, upload/text/meta and the row."""
    import asyncio

    from src.api.db.database import AudioJobsRepository
    from src.api.services.audio_cleanup import delete_job_artifacts

    run_id = "doc-tts-delete1"
    audio_dir = Path(settings.doc_tts_dir) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / f"{run_id}.mp3").write_bytes(b"ID3")
    (audio_dir / f"{run_id}.meta.json").write_text('{"sha256": "x"}')
    upload = Path(settings.doc_tts_dir) / "uploads"
    upload.mkdir(parents=True, exist_ok=True)
    (upload / f"{run_id}.txt").write_text("fuente")
    text = Path(settings.doc_tts_dir) / "text"
    text.mkdir(exist_ok=True)
    (text / f"{run_id}.txt").write_text("extracto")
    meta = Path(settings.doc_tts_dir) / "meta"
    meta.mkdir(exist_ok=True)
    (meta / f"{run_id}.json").write_text('{"title": "doc"}')

    async def _seed() -> None:
        await AudioJobsRepository().start_job(run_id, collection="documents", kind="doc_tts")
        await AudioJobsRepository().finish_job(
            run_id,
            status="FINISHED_OK",
            audio_path=str(audio_dir / f"{run_id}.mp3"),
            kind="doc_tts",
        )

    asyncio.run(_seed())

    client = _client()
    with patch(
        "src.api.routes_ui.delete_job_artifacts",
        side_effect=delete_job_artifacts,
    ):
        response = client.post(f"/ui/tools/jobs/doc_tts/{run_id}/delete", follow_redirects=False)

    assert response.status_code == 302
    assert "Borrado" in _flash_of(response.headers["location"])
    assert not (audio_dir / f"{run_id}.mp3").exists(), "the audio file is gone"
    assert not (audio_dir / f"{run_id}.meta.json").exists(), "the sidecar is gone"
    assert not (text / f"{run_id}.txt").exists(), "the extracted text is gone"
    assert not (meta / f"{run_id}.json").exists(), "the meta json is gone"
    assert not (upload / f"{run_id}.txt").exists(), "the upload is gone"

    async def _check() -> None:
        assert await AudioJobsRepository().get_job(run_id, kind="doc_tts") is None

    asyncio.run(_check())


def test_delete_narration_forgets_the_runs_narration_path(_ui_env: Path) -> None:
    """Deleting a narration also clears digest_runs.narration_path."""
    import asyncio

    from src.api.db.database import AudioJobsRepository, RunsRepository

    run_id = "digest-pipeline-delete-2"
    audio_dir = Path(settings.narration_cache_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "narrate_tech_261008_x.wav").write_bytes(b"WAV")

    async def _seed() -> None:
        await RunsRepository().record_run(
            workflow_id=run_id,
            collection="tech",
            status="ok",
            article_count=1,
            cluster_count=1,
            digest_preview="# X",
        )
        await RunsRepository().set_narration_path(
            run_id, str(audio_dir / "narrate_tech_261008_x.wav")
        )
        await AudioJobsRepository().start_job(run_id, collection="tech", kind="narration")
        await AudioJobsRepository().finish_job(
            run_id,
            status="FINISHED_OK",
            audio_path=str(audio_dir / "narrate_tech_261008_x.wav"),
            kind="narration",
        )

    asyncio.run(_seed())

    client = _client()
    response = client.post(f"/ui/tools/jobs/narration/{run_id}/delete", follow_redirects=False)

    assert response.status_code == 302
    assert not (audio_dir / "narrate_tech_261008_x.wav").exists(), "the narration audio is gone"

    async def _check() -> None:
        assert await AudioJobsRepository().get_job(run_id, kind="narration") is None
        run = await RunsRepository().get_run_by_workflow_id(run_id)
        assert bool(run) and not run.get("narration_path")

    asyncio.run(_check())


def test_delete_job_refuses_a_foreign_id(_ui_env: Path) -> None:
    """A run id outside the safe alphabet never reaches the filesystem."""
    client = _client()

    response = client.post("/ui/tools/jobs/doc_tts/../../etc-x/delete", follow_redirects=False)

    assert response.status_code == 404, "the router does not match traversal ids"
    assert not client.get("/ui/fragments/tool_jobs?kind=doc_tts").status_code == 500


def test_audiobook_submit_forwards_engine_and_voice() -> None:
    """The audiobook form delegates to submit_audiobook with the chosen motor."""
    client = _client()
    result = {
        "workflow_id": "audiobook-abc",
        "title": "Mi libro",
        "filename": "libro.epub",
        "kind": "epub",
        "status_url": "/api/admin/audiobooks/audiobook-abc",
    }
    with patch(
        "src.api.services.audiobook.submit_audiobook",
        new_callable=AsyncMock,
        return_value=result,
    ) as submit:
        response = client.post(
            "/ui/tools/audiobooks",
            files={"file": ("libro.epub", b"PK-fake", "application/epub+zip")},
            data={
                "title": "Mi libro",
                "voice": "es-mx-male",
                "language": "es",
                "engine": "kokoro",
            },
            follow_redirects=False,
        )

    assert "audiobook-abc" in _flash_of(response.headers["location"])
    kwargs = submit.call_args.kwargs
    assert kwargs["engine"] == "kokoro"
    assert kwargs["voice"] == "es-mx-male"
    assert kwargs["language"] == "es"


def test_audiobook_form_offers_only_local_engines() -> None:
    """The audiobook engine select bans Modal and labels qwen3 as local."""
    client = _client()
    response = client.get("/ui/tools")

    assert response.status_code == 200
    select = response.text.split('id="audiobook-engine"', 1)[1].split("</select>", 1)[0]
    assert 'value="edge"' in select
    assert 'value="omni_voice"' in select
    assert 'value="kokoro"' in select
    assert 'value="qwen3_tts"' in select
    assert "Qwen3-TTS (local gateway)" in select
    assert 'value="zonos2"' not in select, "Modal-only engines never render a book"
    assert 'value="voxcpm2"' not in select


def test_job_fragment_lists_finished_audiobooks(_ui_env: Path) -> None:
    """Audiobook rows are listed with their title and download link."""
    import asyncio

    from src.api.db.database import AudioJobsRepository
    from src.api.services.doc_tts import write_job_meta

    run_id = "audiobook-fragment1"
    audio = Path(settings.audiobook_dir) / "audio" / f"{run_id}.m4b"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake-m4b")

    async def _seed() -> None:
        await AudioJobsRepository().start_job(run_id, collection="audiobooks", kind="audiobook")
        await AudioJobsRepository().finish_job(
            run_id,
            status="FINISHED_OK",
            audio_path=str(audio),
            kind="audiobook",
        )

    write_job_meta(run_id, {"title": "Mi audiolibro", "mode": "edge"})
    asyncio.run(_seed())

    response = _client().get("/ui/fragments/tool_jobs?kind=audiobook")

    assert response.status_code == 200
    assert "Mi audiolibro" in response.text
    assert f"/api/admin/audiobooks/{run_id}/download" in response.text


def test_delete_audiobook_removes_audio_cover_upload_and_meta(_ui_env: Path) -> None:
    """Deleting an audiobook purges every artifact and its job row."""
    import asyncio

    from src.api.db.database import AudioJobsRepository

    run_id = "audiobook-delete1"
    root = Path(settings.audiobook_dir)
    audio = root / "audio" / f"{run_id}.m4b"
    cover = root / "covers" / f"{run_id}.jpg"
    upload = Path(settings.doc_tts_dir) / "uploads" / f"{run_id}.epub"
    meta = Path(settings.doc_tts_dir) / "meta" / f"{run_id}.json"
    for path in (audio, cover, upload, meta):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")

    async def _seed() -> None:
        await AudioJobsRepository().start_job(run_id, collection="audiobooks", kind="audiobook")
        await AudioJobsRepository().finish_job(
            run_id,
            status="FINISHED_OK",
            audio_path=str(audio),
            kind="audiobook",
        )

    asyncio.run(_seed())

    response = _client().post(f"/ui/tools/jobs/audiobook/{run_id}/delete", follow_redirects=False)

    assert response.status_code == 302
    assert "Borrado" in _flash_of(response.headers["location"])
    for path in (audio, cover, upload, meta):
        assert not path.exists(), f"{path} must be deleted"

    async def _check() -> None:
        assert await AudioJobsRepository().get_job(run_id, kind="audiobook") is None

    asyncio.run(_check())
