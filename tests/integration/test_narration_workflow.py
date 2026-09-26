"""Integration tests for the Phase 7 narration workflow and its cross-queue dispatch."""

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.narration_workflows import (
    RENDER_NARRATION_ACTIVITY,
    RESOLVE_SCRIPT_ACTIVITY,
    NarrationConfig,
    NarrationWorkflow,
)
from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig

NARRATION_QUEUE = "news-engine-narration-task-queue"
DIGEST_QUEUE = "news-engine-task-queue"

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
RENDERED: dict[str, Any] = {
    "audio_path": "/tmp/narration/narrate_tech_260926_abcd1234.m4a",
    "wav_path": "/tmp/narration/narrate_tech_260926_abcd1234.wav",
    "duration_seconds": 12.5,
    "cached": False,
    "segments": 2,
    "char_count": 35,
    "voices": ["es-mx-male", "es-mx-female"],
    "used_conversations_api": False,
    "edge_fallbacks": 0,
    "music": None,
    "title": "Edicion",
}

#: Mutable state shared with the stub activities.
SEEN: dict[str, Any] = {}


def _reset(render_error: BaseException | None = None) -> None:
    SEEN.clear()
    SEEN.update(
        {
            "render": [],
            "resolve": [],
            "record": [],
            "render_error": render_error,
        }
    )


@activity.defn(name=RESOLVE_SCRIPT_ACTIVITY)
async def stub_resolve(payload: dict) -> dict:
    SEEN["resolve"].append(payload)
    return {
        "script": payload.get("script"),
        "source": "manual",
        "title": "Edicion",
        "segments": 2,
        "char_count": 35,
    }


@activity.defn(name=RENDER_NARRATION_ACTIVITY)
async def stub_render(payload: dict) -> dict:
    SEEN["render"].append(payload)
    if SEEN["render_error"] is not None:
        raise SEEN["render_error"]
    return RENDERED


# --- Digest-side stubs (same names as src/workers/activities.py) -------------

DIGEST = {
    "status": "ok",
    "digest": "# Top Stories\n\n- Something happened",
    "article_count": 1,
}
ARTICLE = {
    "title": "Something happened",
    "link": "https://example.com/a",
    "content": "Body",
    "source": "Example",
}
CLUSTER = {"cluster_id": 1, "fingerprint": {}, "articles": [ARTICLE]}


@activity.defn(name="fetch_rss_activity")
async def stub_fetch(feed_urls: list[str]) -> list[dict]:
    return [ARTICLE]


@activity.defn(name="filter_new_activity")
async def stub_filter(articles: list[dict], collection: str = "default") -> list[dict]:
    return articles


@activity.defn(name="cluster_activity")
async def stub_cluster(articles: list[dict], collection: str = "default") -> list[dict]:
    return [CLUSTER]


@activity.defn(name="generate_digest_activity")
async def stub_digest(payload: dict) -> dict:
    return DIGEST


@activity.defn(name="deliver_telegram_message_activity")
async def stub_deliver(text: str, chat_id: str | None = None) -> dict:
    return {"status": "ok", "message_id": 1}


@activity.defn(name="record_run_activity")
async def stub_record(run: dict) -> dict:
    SEEN["record"].append(run)
    return {"status": "ok", "run_id": 1}


def _digest_worker(client: Any, queue: str) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        workflows=[DigestPipelineWorkflow],
        activities=[
            stub_fetch,
            stub_filter,
            stub_cluster,
            stub_digest,
            stub_deliver,
            stub_record,
        ],
    )


def _narration_worker(client: Any) -> Worker:
    return Worker(
        client,
        task_queue=NARRATION_QUEUE,
        workflows=[NarrationWorkflow],
        activities=[stub_resolve, stub_render],
    )


async def test_narration_workflow_renders_the_resolved_script() -> None:
    _reset()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _narration_worker(env.client):
            result = await env.client.execute_workflow(
                NarrationWorkflow.run,
                NarrationConfig(collection="tech", spec=SPEC, script=SCRIPT, run_id="digest-1"),
                id="narration-tech-1",
                task_queue=NARRATION_QUEUE,
            )

    assert result["status"] == "ok"
    assert result["run_id"] == "digest-1"
    assert result["narration"]["audio_path"] == RENDERED["audio_path"]
    assert len(SEEN["resolve"]) == 1
    assert len(SEEN["render"]) == 1
    assert SEEN["render"][0]["run_id"] == "digest-1"
    assert SEEN["render"][0]["collection"] == "tech"
    assert SEEN["render"][0]["strategy"] == "turns"
    assert SEEN["render"][0]["script"]["segments"][0]["role"] == "anchor"


async def test_narration_workflow_falls_back_to_its_own_workflow_id() -> None:
    """A standalone run keys its audio job on the narration workflow id."""
    _reset()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _narration_worker(env.client):
            result = await env.client.execute_workflow(
                NarrationWorkflow.run,
                NarrationConfig(collection="tech", spec=SPEC, script=SCRIPT),
                id="narration-standalone",
                task_queue=NARRATION_QUEUE,
            )

    assert result["run_id"] == "narration-standalone"
    assert SEEN["render"][0]["run_id"] == "narration-standalone"


async def test_narration_workflow_passes_the_strategy_through() -> None:
    _reset()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _narration_worker(env.client):
            await env.client.execute_workflow(
                NarrationWorkflow.run,
                NarrationConfig(
                    collection="tech", spec=SPEC, script=SCRIPT, strategy="conversations"
                ),
                id="narration-conv",
                task_queue=NARRATION_QUEUE,
            )

    assert SEEN["render"][0]["strategy"] == "conversations"


async def test_narration_workflow_rejects_an_empty_spec() -> None:
    _reset()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with _narration_worker(env.client):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    NarrationWorkflow.run,
                    NarrationConfig(collection="tech"),
                    id="narration-empty",
                    task_queue=NARRATION_QUEUE,
                )
    assert "needs a 'spec' payload" in str(excinfo.value.cause)

    assert SEEN["render"] == []


async def test_digest_dispatches_narration_to_the_narration_queue() -> None:
    """The digest worker must reach the narration activity by name on its queue."""
    _reset()
    config = PipelineConfig(
        feed_urls=["https://example.com/rss"],
        tts_enabled=False,
        deliver_audio=False,
        collection="tech",
        delivery_channels=["telegram"],
        narration={"task_queue": NARRATION_QUEUE, "spec": SPEC, "script": SCRIPT},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            _narration_worker(env.client),
            _digest_worker(env.client, DIGEST_QUEUE),
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                config,
                id="digest-narrated",
                task_queue=DIGEST_QUEUE,
            )

    assert result["status"] == "ok"
    assert len(SEEN["render"]) == 1
    assert SEEN["render"][0]["run_id"] == "digest-narrated"
    assert SEEN["render"][0]["collection"] == "tech"
    narration_delivery = [d for d in result["delivery_status"] if d.get("channel") == "narration"]
    assert narration_delivery == [
        {"channel": "narration", "status": "ok", "audio_path": RENDERED["audio_path"]}
    ]


async def test_digest_records_the_run_before_dispatching_the_narration() -> None:
    """The narration worker writes digest_runs.narration_path, so the row must
    already exist when the cross-queue activity runs."""
    _reset()
    config = PipelineConfig(
        feed_urls=["https://example.com/rss"],
        tts_enabled=False,
        deliver_audio=False,
        collection="tech",
        delivery_channels=["telegram"],
        narration={"task_queue": NARRATION_QUEUE, "spec": SPEC, "script": SCRIPT},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            _narration_worker(env.client),
            _digest_worker(env.client, DIGEST_QUEUE),
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                config,
                id="digest-narration-order",
                task_queue=DIGEST_QUEUE,
            )

    assert len(SEEN["record"]) == 1
    assert SEEN["record"][0]["status"] == "ok"
    assert SEEN["record"][0]["collection"] == "tech"
    assert SEEN["render"][0]["run_id"] == "digest-narration-order"


async def test_digest_survives_a_failing_narration() -> None:
    """A narration failure is recorded but never fails the digest."""
    _reset(render_error=ApplicationError("gateway down", non_retryable=True))
    config = PipelineConfig(
        feed_urls=["https://example.com/rss"],
        tts_enabled=False,
        deliver_audio=False,
        collection="tech",
        delivery_channels=["telegram"],
        narration={"task_queue": NARRATION_QUEUE, "spec": SPEC, "script": SCRIPT},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            _narration_worker(env.client),
            _digest_worker(env.client, DIGEST_QUEUE),
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                config,
                id="digest-narration-fails",
                task_queue=DIGEST_QUEUE,
            )

    assert result["status"] == "ok"
    narration_delivery = [d for d in result["delivery_status"] if d.get("channel") == "narration"]
    assert narration_delivery and narration_delivery[0]["status"] == "error"


async def test_digest_without_narration_never_calls_the_narration_queue() -> None:
    _reset()
    config = PipelineConfig(
        feed_urls=["https://example.com/rss"],
        tts_enabled=False,
        deliver_audio=False,
        collection="tech",
        delivery_channels=["telegram"],
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with (
            _narration_worker(env.client),
            _digest_worker(env.client, DIGEST_QUEUE),
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                config,
                id="digest-plain",
                task_queue=DIGEST_QUEUE,
            )

    assert result["status"] == "ok"
    assert SEEN["render"] == []
    assert [d for d in result["delivery_status"] if d.get("channel") == "narration"] == []


def test_narration_payload_requires_a_task_queue_and_spec() -> None:
    """A malformed opt-in fails loudly instead of dispatching nowhere."""
    with pytest.raises(ApplicationError, match="task_queue"):
        DigestPipelineWorkflow._narration_payload(PipelineConfig(narration={"spec": SPEC}), "r")
    with pytest.raises(Exception, match="task_queue"):
        DigestPipelineWorkflow._narration_payload(
            PipelineConfig(narration={"task_queue": NARRATION_QUEUE}), "r"
        )


def test_narration_payload_merges_the_opt_in() -> None:
    request = DigestPipelineWorkflow._narration_payload(
        PipelineConfig(
            collection="tech",
            narration={
                "task_queue": NARRATION_QUEUE,
                "spec": SPEC,
                "script": SCRIPT,
                "script_id": None,
                "strategy": "conversations",
            },
        ),
        "digest-42",
    )
    assert request["task_queue"] == NARRATION_QUEUE
    assert request["payload"] == {
        "run_id": "digest-42",
        "collection": "tech",
        "spec": SPEC,
        "script": SCRIPT,
        "script_id": None,
        "strategy": "conversations",
    }
