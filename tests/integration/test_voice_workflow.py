"""Integration tests for VoiceCatalogWorkflow via WorkflowEnvironment."""

from typing import Any

from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from src.workers.voice_workflows import (
    DESIGN_VOICE_ACTIVITY,
    UPLOAD_VOICE_ACTIVITY,
    VOICE_STATUS_ACTIVITY,
    VoiceCatalogConfig,
    VoiceCatalogWorkflow,
)

QUEUE = "voice-test-queue"
SEEN: dict[str, list[dict[str, Any]]] = {"design": [], "upload": []}
FAIL: dict[str, str] = {}
PROFILES: list[dict[str, Any]] = []


def _profile(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": f"Voice {name}",
        "instruct": "Male, late 40s, warm baritone, broadcast clarity",
        "ref_text": "Welcome to the daily briefing. " * 6,
        "language": "en",
    }


@activity.defn(name=VOICE_STATUS_ACTIVITY)
async def stub_status(payload: dict) -> dict:
    names = payload.get("names") or []
    targets = [p for p in PROFILES if not names or p["name"] in names]
    return {
        "generated": [],
        "missing": [p["name"] for p in targets],
        "stale": [p["name"] for p in targets],
        "pending": [p["name"] for p in targets],
        "profiles": targets,
    }


@activity.defn(name=DESIGN_VOICE_ACTIVITY)
async def stub_design(payload: dict) -> dict:
    SEEN["design"].append(payload)
    if payload["name"] in FAIL:
        raise RuntimeError(FAIL[payload["name"]])
    return {
        "name": payload["name"],
        "file": f"refs/{payload['name']}.wav",
        "sha256": "a" * 64,
        "sample_rate": 24000,
        "duration_seconds": 15.0,
    }


@activity.defn(name=UPLOAD_VOICE_ACTIVITY)
async def stub_upload(payload: dict) -> dict:
    SEEN["upload"].append(payload)
    return {"name": payload["name"], "uploaded": True}


def _reset(profiles: list[dict[str, Any]]) -> None:
    SEEN["design"].clear()
    SEEN["upload"].clear()
    FAIL.clear()
    PROFILES.clear()
    PROFILES.extend(profiles)


def _worker(client: Any) -> Any:
    from temporalio.worker import Worker

    return Worker(
        client,
        task_queue=QUEUE,
        workflows=[VoiceCatalogWorkflow],
        activities=[stub_status, stub_design, stub_upload],
    )


async def test_voice_catalog_workflow_designs_pending_profiles() -> None:
    _reset([_profile("a"), _profile("b")])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env.client):
            result = await env.client.execute_workflow(
                VoiceCatalogWorkflow.run,
                VoiceCatalogConfig(task_queue=QUEUE),
                id="voice-catalog-1",
                task_queue=QUEUE,
            )

    assert sorted(result["pending"]) == ["a", "b"]
    assert len(SEEN["design"]) == 2
    assert all(r["status"] == "ok" for r in result["results"])
    assert SEEN["upload"] == []


async def test_voice_catalog_workflow_isolates_failures() -> None:
    _reset([_profile("a"), _profile("b")])
    FAIL["b"] = "modal exploded"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env.client):
            result = await env.client.execute_workflow(
                VoiceCatalogWorkflow.run,
                VoiceCatalogConfig(task_queue=QUEUE),
                id="voice-catalog-2",
                task_queue=QUEUE,
            )

    by_name = {r["name"]: r for r in result["results"]}
    assert by_name["a"]["status"] == "ok"
    assert by_name["b"]["status"] == "error"
    assert by_name["b"]["error"]


async def test_voice_catalog_workflow_uploads_when_requested() -> None:
    _reset([_profile("a")])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env.client):
            await env.client.execute_workflow(
                VoiceCatalogWorkflow.run,
                VoiceCatalogConfig(upload=True, task_queue=QUEUE),
                id="voice-catalog-3",
                task_queue=QUEUE,
            )

    assert len(SEEN["upload"]) == 1
    assert SEEN["upload"][0]["name"] == "a"


async def test_voice_catalog_workflow_honours_the_name_subset() -> None:
    _reset([_profile("a"), _profile("b")])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _worker(env.client):
            result = await env.client.execute_workflow(
                VoiceCatalogWorkflow.run,
                VoiceCatalogConfig(names=["a"], task_queue=QUEUE),
                id="voice-catalog-4",
                task_queue=QUEUE,
            )

    assert result["pending"] == ["a"]
    assert len(SEEN["design"]) == 1
