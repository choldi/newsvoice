"""Unit tests for the Phase 7 narration Temporal activities."""

from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.api.db.database import AudioJobsRepository, NarrationRepository, RunsRepository
from src.api.models.narration import NarrationScript, NarrationSpec
from src.workers import narration_activities

SPEC: dict[str, Any] = {
    "title": "Edicion",
    "roles": [
        {"name": "anchor", "voice_id": "es-mx-male"},
        {"name": "guest", "voice_id": "es-mx-female"},
    ],
    "pause_ms": 400,
}
SCRIPT: dict[str, Any] = {
    "title": "Edicion",
    "source": "manual",
    "segments": [
        {"role": "anchor", "text": "Bienvenidos."},
        {"role": "guest", "text": "Gracias por invitarnos."},
    ],
}
RENDER_RESULT: dict[str, Any] = {
    "audio_path": "/tmp/narration/narrate_tech_260926_abcd1234.m4a",
    "wav_path": "/tmp/narration/narrate_tech_260926_abcd1234.wav",
    "duration_seconds": 12.5,
    "cached": False,
    "segments": 2,
    "char_count": 34,
    "voices": ["es-mx-male", "es-mx-female"],
    "used_conversations_api": False,
    "edge_fallbacks": 0,
    "music": None,
    "title": "Edicion",
}


class _FakeService:
    """NarrationTTSService stand-in recording how it was called."""

    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def render(
        self,
        spec: NarrationSpec,
        script: NarrationScript,
        collection: str = "default",
        day: Any = None,
        strategy: Any = None,
    ) -> Any:
        self.calls.append(
            {
                "spec": spec,
                "script": script,
                "collection": collection,
                "strategy": str(strategy),
            }
        )
        if self.error is not None:
            raise self.error
        from src.api.models.narration import NarrationOut

        return NarrationOut.model_validate(self.result)


def _patch_service(monkeypatch: pytest.MonkeyPatch, service: _FakeService) -> None:
    monkeypatch.setattr(
        narration_activities, "NarrationTTSService", lambda *a, **k: service, raising=True
    )


async def test_render_activity_records_the_job_and_the_run_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    await RunsRepository().record_run(
        workflow_id="digest-1",
        collection="tech",
        status="ok",
        article_count=3,
        cluster_count=2,
    )
    service = _FakeService(RENDER_RESULT)
    _patch_service(monkeypatch, service)
    payload = {
        "run_id": "digest-1",
        "collection": "tech",
        "spec": SPEC,
        "script": SCRIPT,
        "strategy": "turns",
    }

    result = await ActivityEnvironment().run(
        narration_activities.render_narration_activity, payload
    )

    assert result["audio_path"] == RENDER_RESULT["audio_path"]
    assert result["run_attached"] is True
    assert result["duration_seconds"] == 12.5
    job = await AudioJobsRepository().get_job("digest-1", kind="narration")
    assert job is not None
    assert job["status"] == "FINISHED_OK"
    assert job["audio_path"] == RENDER_RESULT["audio_path"]
    assert job["elapsed_seconds"] is not None
    # The digest TTS job must be untouched by the narration job.
    assert await AudioJobsRepository().get_job("digest-1") is None
    runs = await RunsRepository().list_runs(collection="tech")
    assert runs[0]["narration_path"] == RENDER_RESULT["audio_path"]
    assert runs[0]["audio_path"] is None
    assert service.calls[0]["strategy"] == "turns"
    assert service.calls[0]["collection"] == "tech"


async def test_render_activity_uses_the_conversations_strategy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    service = _FakeService(RENDER_RESULT)
    _patch_service(monkeypatch, service)

    await ActivityEnvironment().run(
        narration_activities.render_narration_activity,
        {
            "run_id": "r",
            "collection": "tech",
            "spec": SPEC,
            "script": SCRIPT,
            "strategy": "conversations",
        },
    )

    assert service.calls[0]["strategy"] == "conversations"


async def test_render_activity_defaults_to_the_turns_strategy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    service = _FakeService(RENDER_RESULT)
    _patch_service(monkeypatch, service)

    await ActivityEnvironment().run(
        narration_activities.render_narration_activity,
        {"run_id": "r", "collection": "tech", "spec": SPEC, "script": SCRIPT},
    )

    assert service.calls[0]["strategy"] == "turns"


async def test_render_activity_rejects_an_unknown_strategy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    _patch_service(monkeypatch, _FakeService(RENDER_RESULT))

    with pytest.raises(ApplicationError, match="Unknown render strategy"):
        await ActivityEnvironment().run(
            narration_activities.render_narration_activity,
            {
                "run_id": "r",
                "collection": "tech",
                "spec": SPEC,
                "script": SCRIPT,
                "strategy": "karaoke",
            },
        )


async def test_render_activity_marks_the_job_as_failed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    from src.api.services.narration_tts import NarrationRenderError

    _patch_service(monkeypatch, _FakeService(error=NarrationRenderError("bad music")))

    with pytest.raises(ApplicationError, match="Narration render failed"):
        await ActivityEnvironment().run(
            narration_activities.render_narration_activity,
            {"run_id": "digest-2", "collection": "tech", "spec": SPEC, "script": SCRIPT},
        )

    job = await AudioJobsRepository().get_job("digest-2", kind="narration")
    assert job is not None
    assert job["status"] == "FINISHED_ERROR"
    assert "bad music" in job["error"]


async def test_render_activity_rejects_a_script_with_an_unknown_role(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    service = _FakeService(RENDER_RESULT)
    _patch_service(monkeypatch, service)
    bad_script = {**SCRIPT, "segments": [{"role": "narrador", "text": "Hola."}]}

    with pytest.raises(ApplicationError, match="Invalid narration spec/script") as excinfo:
        await ActivityEnvironment().run(
            narration_activities.render_narration_activity,
            {"run_id": "r", "collection": "tech", "spec": SPEC, "script": bad_script},
        )

    assert excinfo.value.non_retryable is True
    assert service.calls == []


async def test_render_activity_needs_both_spec_and_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    _patch_service(monkeypatch, _FakeService(RENDER_RESULT))

    with pytest.raises(ApplicationError, match="both 'spec' and 'script'"):
        await ActivityEnvironment().run(
            narration_activities.render_narration_activity,
            {"run_id": "r", "collection": "tech", "spec": SPEC},
        )


async def test_render_activity_survives_a_run_without_a_digest_row(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    _patch_service(monkeypatch, _FakeService(RENDER_RESULT))

    result = await ActivityEnvironment().run(
        narration_activities.render_narration_activity,
        {"run_id": "standalone", "collection": "tech", "spec": SPEC, "script": SCRIPT},
    )

    assert result["audio_path"] == RENDER_RESULT["audio_path"]
    assert result["run_attached"] is False
    job = await AudioJobsRepository().get_job("standalone", kind="narration")
    assert job is not None and job["status"] == "FINISHED_OK"


async def test_resolve_activity_validates_an_inline_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))

    resolved = await ActivityEnvironment().run(
        narration_activities.resolve_narration_script_activity,
        {"spec": SPEC, "script": SCRIPT, "collection": "tech"},
    )

    assert resolved["source"] == "manual"
    assert resolved["segments"] == 2
    assert resolved["char_count"] == 35
    assert resolved["script"]["segments"][0]["role"] == "anchor"


async def test_resolve_activity_loads_a_stored_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    spec = NarrationSpec.model_validate(SPEC)
    script = NarrationScript.model_validate(SCRIPT, context={"spec": spec})
    script_id = await NarrationRepository().save_script(
        run_id="digest-9", script=script, collection="tech"
    )

    resolved = await ActivityEnvironment().run(
        narration_activities.resolve_narration_script_activity,
        {"spec": SPEC, "script_id": script_id, "collection": "tech"},
    )

    assert resolved["segments"] == 2
    assert resolved["script"]["source"] == "manual"


async def test_resolve_activity_fails_on_a_missing_stored_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))

    with pytest.raises(ApplicationError, match="not found") as excinfo:
        await ActivityEnvironment().run(
            narration_activities.resolve_narration_script_activity,
            {"spec": SPEC, "script_id": 4242, "collection": "tech"},
        )

    assert excinfo.value.non_retryable is True


async def test_resolve_activity_needs_a_script_or_a_script_id_or_existing_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no script/script_id is provided, the activity tries to generate
    from the digest run. If the run doesn't exist, it fails with a clear error.
    """
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))

    with pytest.raises(ApplicationError, match="No digest run found"):
        await ActivityEnvironment().run(
            narration_activities.resolve_narration_script_activity,
            {"spec": SPEC, "collection": "tech"},
        )


async def test_resolve_activity_generates_script_from_digest_when_no_script_provided(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Placeholder for a test that mocks the LLM generation.
    Currently the generation path calls the real LLM, so this test is not run.
    """
    pass


async def test_resolve_activity_rejects_an_invalid_spec(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "news.db"))
    bad_spec = {**SPEC, "roles": [{"name": "anchor", "voice_id": "es-mx-male", "speed": 9}]}

    with pytest.raises(ApplicationError, match="Invalid narration spec"):
        await ActivityEnvironment().run(
            narration_activities.resolve_narration_script_activity,
            {"spec": bad_spec, "script": SCRIPT},
        )


def test_narration_job_kind_is_not_the_digest_one() -> None:
    """The narration audio job must not reuse the digest 'tts' discriminator."""
    assert narration_activities.NARRATION_JOB_KIND == "narration"
    assert narration_activities.NARRATION_JOB_KIND != "tts"
