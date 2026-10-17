"""Integration tests for the admin REST endpoints (Phase 6 GUI backend)."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import RunsRepository
from src.api.main import app
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _tmp_collections_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.collections_file at an isolated TOML per test."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    return tmp_path / "collections.toml"


def _collection_payload(**overrides: object) -> dict[str, object]:
    """Build a valid collection payload."""
    payload: dict[str, object] = {
        "name": "tech",
        "feed_urls": ["https://example.com/rss"],
        "time": "08:00",
        "tts_enabled": True,
        "deliver_audio": True,
        "podcast_name": "ByteCast",
        "podcast_behavior": "usa un tono fresco",
        "language": "es",
        "voice": "es-mx-male",
        "prompt_template": "default",
        "chat_id": None,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_list_collections_empty() -> None:
    """GET /api/admin/collections returns an empty list for a missing file."""
    response = client.get("/api/admin/collections")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "collections": []}


def test_create_then_list_and_get() -> None:
    """Creating a collection makes it visible in list and get."""
    created = client.post("/api/admin/collections", json=_collection_payload())
    assert created.status_code == 201
    assert created.json()["status"] == "ok"
    assert created.json()["collection"]["name"] == "tech"
    assert created.json()["collection"]["podcast_name"] == "ByteCast"

    listed = client.get("/api/admin/collections")
    assert len(listed.json()["collections"]) == 1

    fetched = client.get("/api/admin/collections/tech")
    assert fetched.status_code == 200
    assert fetched.json()["collection"]["time"] == "08:00"


def test_logic_delivery_round_trip_via_api() -> None:
    """logic_delivery persists through create/list/get with the admin API."""
    payload = _collection_payload(
        logic_delivery={
            "channels": ["email"],
            "email_recipients": ["jordias@msn.com"],
        }
    )
    created = client.post("/api/admin/collections", json=payload)
    assert created.status_code == 201
    logic = created.json()["collection"]["logic_delivery"]
    assert logic["channels"] == ["email"]
    assert logic["email_recipients"] == ["jordias@msn.com"]

    fetched = client.get("/api/admin/collections/tech")
    assert fetched.status_code == 200
    assert fetched.json()["collection"]["logic_delivery"]["channels"] == ["email"]


def test_whatsapp_round_trip_via_api() -> None:
    """WhatsApp fields persist through create/get/put with the admin API."""
    payload = _collection_payload(
        delivery_channels=["telegram", "whatsapp"],
        whatsapp_recipients=["+34600111222", "+34600333444"],
        whatsapp_group_id="1234567890",
        logic_delivery={
            "channels": ["whatsapp"],
            "whatsapp_recipients": ["+34600555555"],
            "whatsapp_group_id": "9876543210",
        },
    )
    created = client.post("/api/admin/collections", json=payload)
    assert created.status_code == 201
    collection = created.json()["collection"]
    assert collection["whatsapp_recipients"] == ["+34600111222", "+34600333444"]
    assert collection["whatsapp_group_id"] == "1234567890"
    assert collection["delivery_channels"] == ["telegram", "whatsapp"]
    assert collection["logic_delivery"]["whatsapp_recipients"] == ["+34600555555"]
    assert collection["logic_delivery"]["whatsapp_group_id"] == "9876543210"

    fetched = client.get("/api/admin/collections/tech").json()["collection"]
    assert fetched["whatsapp_recipients"] == ["+34600111222", "+34600333444"]
    assert fetched["whatsapp_group_id"] == "1234567890"
    assert fetched["logic_delivery"]["whatsapp_recipients"] == ["+34600555555"]

    updated = client.put(
        "/api/admin/collections/tech",
        json={**payload, "podcast_name": "Otro nombre"},
    )
    assert updated.status_code == 200
    assert updated.json()["collection"]["whatsapp_recipients"] == [
        "+34600111222",
        "+34600333444",
    ]
    assert updated.json()["collection"]["logic_delivery"]["whatsapp_group_id"] == "9876543210"


def test_create_with_invalid_whatsapp_number_returns_422() -> None:
    """A non-E.164 WhatsApp number is rejected with 422."""
    payload = _collection_payload(whatsapp_recipients=["600111222"])
    response = client.post("/api/admin/collections", json=payload)
    assert response.status_code == 422


def test_create_with_unknown_field_returns_422() -> None:
    """Unknown fields are rejected instead of being silently dropped."""
    response = client.post(
        "/api/admin/collections",
        json=_collection_payload(whatsapp_sender="+34600000000"),
    )
    assert response.status_code == 422


def test_create_duplicate_conflicts() -> None:
    """Creating a duplicate collection returns 409."""
    client.post("/api/admin/collections", json=_collection_payload())
    response = client.post("/api/admin/collections", json=_collection_payload())
    assert response.status_code == 409


def test_create_invalid_time_returns_422() -> None:
    """An invalid HH:MM time is rejected with 422."""
    payload = _collection_payload(time="25:99")
    response = client.post("/api/admin/collections", json=payload)
    assert response.status_code == 422


def test_get_missing_collection_404() -> None:
    """GET of an unknown collection returns 404."""
    response = client.get("/api/admin/collections/nope")
    assert response.status_code == 404


def test_update_collection() -> None:
    """PUT updates the stored fields (and can rename with a fresh payload)."""
    client.post("/api/admin/collections", json=_collection_payload())

    renamed = _collection_payload(name="tech-v2", podcast_name="New Name", time="17:15")
    response = client.put("/api/admin/collections/tech", json=renamed)
    assert response.status_code == 200
    assert response.json()["collection"]["name"] == "tech-v2"

    old = client.get("/api/admin/collections/tech")
    assert old.status_code == 404
    new = client.get("/api/admin/collections/tech-v2")
    assert new.status_code == 200
    assert new.json()["collection"]["podcast_name"] == "New Name"


def test_update_missing_collection_404() -> None:
    """PUT of an unknown collection returns 404."""
    response = client.put("/api/admin/collections/nope", json=_collection_payload())
    assert response.status_code == 404


def test_delete_collection() -> None:
    """DELETE removes the collection from the file."""
    client.post("/api/admin/collections", json=_collection_payload())
    response = client.delete("/api/admin/collections/tech")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    gone = client.get("/api/admin/collections/tech")
    assert gone.status_code == 404


def test_delete_missing_collection_404() -> None:
    """DELETE of an unknown collection returns 404."""
    response = client.delete("/api/admin/collections/nope")
    assert response.status_code == 404


def test_runs_list_and_get(tmp_path: Path) -> None:
    """Recorded runs are returned by the runs endpoints."""
    repo = RunsRepository(db_path=settings.database_path)

    async def _seed() -> None:
        await repo.record_run(
            workflow_id="wf-1",
            collection="tech",
            status="ok",
            article_count=3,
            cluster_count=2,
            digest_preview="# Digest",
            audio_path=None,
            error=None,
        )

    import asyncio

    asyncio.run(_seed())

    response = client.get("/api/admin/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["total"] == 1
    assert body["runs"][0]["collection"] == "tech"

    filtered = client.get("/api/admin/runs", params={"collection": "tech"})
    assert filtered.json()["total"] == 1

    detail = client.get(f"/api/admin/runs/{body['runs'][0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["run"]["status"] == "ok"


def test_runs_missing_404() -> None:
    """Unknown run id returns 404."""
    response = client.get("/api/admin/runs/9999")
    assert response.status_code == 404


def test_run_audio_status() -> None:
    """The audio job status endpoint returns TTS generation progress."""
    from src.api.db.database import AudioJobsRepository

    async def _seed() -> None:
        await RunsRepository(db_path=settings.database_path).record_run(
            workflow_id="wf-audio",
            collection="tech",
            status="ok",
            article_count=3,
            cluster_count=2,
            digest_preview="# Digest",
        )
        await AudioJobsRepository(db_path=settings.database_path).start_job("wf-audio", "tech")
        await AudioJobsRepository(db_path=settings.database_path).set_generating("wf-audio")
        await AudioJobsRepository(db_path=settings.database_path).finish_job(
            "wf-audio",
            status="FINISHED_OK",
            audio_path="data/tts_cache/z.wav",
            elapsed_seconds=912.0,
        )

    import asyncio

    asyncio.run(_seed())

    runs = client.get("/api/admin/runs").json()["runs"]
    run_id = runs[0]["id"]

    response = client.get(f"/api/admin/runs/{run_id}/audio")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["job"]["run_id"] == "wf-audio"
    assert body["job"]["status"] == "FINISHED_OK"
    assert body["job"]["audio_path"] == "data/tts_cache/z.wav"
    assert body["job"]["elapsed_seconds"] == 912.0


def test_run_audio_missing_404() -> None:
    """A run without a recorded audio job returns 404 on its audio endpoint."""

    async def _seed() -> None:
        await RunsRepository(db_path=settings.database_path).record_run(
            workflow_id="wf-noaudio",
            collection="tech",
            status="ok",
            article_count=1,
            cluster_count=1,
            digest_preview="# Digest",
        )

    import asyncio

    asyncio.run(_seed())

    run_id = client.get("/api/admin/runs").json()["runs"][0]["id"]
    response = client.get(f"/api/admin/runs/{run_id}/audio")
    assert response.status_code == 404


def test_trigger_run_unknown_collection_404() -> None:
    """POST run for an unknown collection returns 404."""
    response = client.post("/api/admin/collections/nope/run")
    assert response.status_code == 404


def test_trigger_run_starts_workflow() -> None:
    """POST run starts the pipeline and returns the workflow id."""
    client.post("/api/admin/collections", json=_collection_payload())
    with patch(
        "src.api.routes.admin_runs.TemporalAdminService.trigger_collection",
        new_callable=AsyncMock,
        return_value="digest-pipeline-tech-20260101120000",
    ):
        response = client.post("/api/admin/collections/tech/run")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["collection"] == "tech"
    assert body["workflow_id"].startswith("digest-pipeline-tech")


def test_resync_schedules() -> None:
    """POST resync returns the schedule reconciliation outcome."""
    with patch(
        "src.api.routes.admin_schedules.TemporalAdminService.resync_schedules",
        new_callable=AsyncMock,
        return_value={"created_or_updated": ["daily-digest-tech"], "removed": []},
    ):
        response = client.post("/api/admin/schedules/resync")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["created_or_updated"] == ["daily-digest-tech"]
    assert body["removed"] == []


def test_voices_stock_list() -> None:
    """GET /api/admin/voices returns the stock voices."""
    with patch(
        "src.api.routes.admin_voices.VoicesService.list_stock",
        new_callable=AsyncMock,
        return_value=[
            {"voice_id": "es-mx-male", "language": "es"},
            {"voice_id": "en-us-female", "language": "en"},
        ],
    ) as mock_list:
        response = client.get("/api/admin/voices")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(response.json()["voices"]) == 2
    mock_list.assert_awaited_once_with(language=None)


def test_voices_cloned_list() -> None:
    """GET /api/admin/voices/cloned returns cloned voices."""
    with patch(
        "src.api.routes.admin_voices.VoicesService.list_cloned",
        new_callable=AsyncMock,
        return_value=[{"voice_id": "mario", "language": "es"}],
    ):
        response = client.get("/api/admin/voices/cloned")

    assert response.status_code == 200
    assert response.json()["voices"][0]["voice_id"] == "mario"


def test_voice_clone_endpoint() -> None:
    """POST /api/admin/voices/clone clones a voice from a base64 audio payload."""
    import base64

    audio_b64 = base64.b64encode(b"RIFFfakeaudio").decode()
    with patch(
        "src.api.routes.admin_voices.VoicesService.clone_voice",
        new_callable=AsyncMock,
        return_value={"voice_id": "mario", "name": "mario", "language": "es"},
    ) as mock_clone:
        response = client.post(
            "/api/admin/voices/clone",
            json={
                "name": "mario",
                "language": "es",
                "audio_base64": audio_b64,
                "filename": "ref.wav",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["voice"]["name"] == "mario"
    mock_clone.assert_awaited_once()
    args, kwargs = mock_clone.call_args
    assert args[0] == "mario"
    assert args[1] == "es"
    assert args[2] == b"RIFFfakeaudio"
    assert kwargs["filename"] == "ref.wav"


def test_voice_clone_invalid_base64_422() -> None:
    """Malformed base64 audio returns 422."""
    response = client.post(
        "/api/admin/voices/clone",
        json={
            "name": "mario",
            "language": "es",
            "audio_base64": "%%%not-base64%%%",
            "filename": "ref.wav",
        },
    )
    assert response.status_code == 422


def test_voice_catalog_status_lists_profiles() -> None:
    """GET /admin/voices/catalog returns the catalog and its status.

    The manifest is patched empty and the custom profiles file is neutralised:
    ``data/voices`` holds state that differs per machine, and this test must
    pin the committed-catalog behaviour deterministically.
    """
    with (
        patch("src.api.services.voice_catalog_admin.load_manifest", return_value={}),
        patch("src.core.voice_catalog.load_custom_profiles", return_value=[]),
    ):
        response = client.get("/api/admin/voices/catalog")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert len(body["profiles"]) == 32
    assert body["generated"] == []
    assert len(body["missing"]) == 32


def test_voice_catalog_generate_starts_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /admin/voices/catalog/generate starts a VoiceCatalogWorkflow."""
    started: dict[str, object] = {}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def start_workflow(self, *args: object, **kwargs: object) -> None:
            started["args"] = args
            started["kwargs"] = kwargs

        async def execute_workflow(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("catalog generation must not block on completion")

    connect = AsyncMock(return_value=_FakeClient())
    monkeypatch.setattr("src.api.services.voice_catalog_admin.build_client", connect)
    response = client.post("/api/admin/voices/catalog/generate", json={"upload": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert "voice-catalog" in body["workflow_id"]
    assert started["kwargs"]["task_queue"] == settings.voice_catalog_task_queue
    assert started["args"][1].design_task_queue == settings.voice_design_task_queue
    connect.assert_awaited_once_with(settings.temporal_voices_namespace)


def test_voice_catalog_generate_wraps_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Temporal failure surfaces as a 502."""

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("temporal down")

    monkeypatch.setattr("src.api.services.voice_catalog_admin.build_client", boom)
    response = client.post("/api/admin/voices/catalog/generate", json={})
    assert response.status_code == 502
    assert "temporal down" in response.json()["detail"]


# --- Phase 8.2: per-collection TTS engine -------------------------------------


def test_tts_backend_round_trips_through_the_api() -> None:
    """The engine survives create, read and update without being dropped."""
    created = client.post(
        "/api/admin/collections", json=_collection_payload(tts_backend="omni_voice")
    )
    assert created.status_code == 201
    assert created.json()["collection"]["tts_backend"] == "omni_voice"

    fetched = client.get("/api/admin/collections/tech").json()["collection"]
    assert fetched["tts_backend"] == "omni_voice"

    updated = client.put(
        "/api/admin/collections/tech",
        # Phase 9.2: the voice must belong to the chosen engine, so the modal
        # update clears the gateway stock id instead of mixing engines.
        json=_collection_payload(tts_backend="qwen3_tts", voice=""),
    )
    assert updated.status_code == 200
    assert updated.json()["collection"]["tts_backend"] == "qwen3_tts"

    reloaded = client.get("/api/admin/collections/tech").json()["collection"]
    assert reloaded["tts_backend"] == "qwen3_tts"


def test_tts_backend_rejects_an_unknown_engine() -> None:
    """A typo is refused at the API instead of failing at 08:00."""
    response = client.post("/api/admin/collections", json=_collection_payload(tts_backend="siri"))

    assert response.status_code == 422
    assert "tts_backend" in response.json()["detail"]


def test_tts_backend_defaults_to_edge() -> None:
    """A payload without the field keeps the current behaviour."""
    created = client.post("/api/admin/collections", json=_collection_payload())

    assert created.status_code == 201
    assert created.json()["collection"]["tts_backend"] == "edge"
