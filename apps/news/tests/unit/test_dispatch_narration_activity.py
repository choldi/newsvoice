"""Unit tests for the cross-namespace narration dispatch activity (Phase 7.9b).

The activity is the seam between the digest namespace and the podcast
namespaces: it starts ``NarrationWorkflow`` in the namespace the backend asks
for and returns the render payload. The Temporal client is faked, so these
tests pin the routing and the idempotency rules rather than re-testing the SDK.
"""

import asyncio
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import ActivityEnvironment

from src.core.config import settings
from src.workers import activities
from src.workers.podcast_target import podcast_target

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
    "segments": [{"role": "anchor", "text": "Bienvenidos."}],
}
RENDER: dict[str, Any] = {
    "audio_path": "/tmp/narration/narrate_tech_1_abcd1234.m4a",
    "duration_seconds": 12.5,
    "segments": 1,
}


class FakeHandle:
    """Workflow handle whose result is already available."""

    def __init__(self, result: dict[str, Any], block: bool = False) -> None:
        self._result = result
        self._block = block
        self.cancelled = False

    async def result(self) -> dict[str, Any]:
        if self._block:
            await asyncio.Event().wait()
        return self._result

    async def cancel(self) -> None:
        self.cancelled = True


class FakeClient:
    """Minimal stand-in for a Temporal client in one namespace."""

    def __init__(self, namespace: str, result: dict[str, Any] | None = None) -> None:
        self.namespace = namespace
        self.started: list[dict[str, Any]] = []
        self.handles: list[FakeHandle] = []
        self._result = result
        self.already_started = False
        self.block_result = False
        self.started_handle: FakeHandle | None = None

    async def start_workflow(self, _workflow: Any, config: Any, **kwargs: Any) -> FakeHandle:
        self.started.append({"config": config, **kwargs})
        if self.already_started:
            raise WorkflowAlreadyStartedError(kwargs["id"], "NarrationWorkflow", run_id="run-1")
        self.started_handle = FakeHandle(
            self._result or {"narration": RENDER}, block=self.block_result
        )
        return self.started_handle

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        self.handles.append(FakeHandle({"narration": RENDER}))
        assert workflow_id
        return self.handles[-1]


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Point build_client at a fake client that records its namespace."""
    client = FakeClient(settings.temporal_podcast_local_namespace)

    async def _build_client(namespace: str | None = None) -> FakeClient:
        client.namespace = namespace or ""
        return client

    monkeypatch.setattr("src.workers.client.build_client", _build_client)
    return client


def _payload(**overrides: Any) -> dict[str, Any]:
    target = podcast_target(SPEC)
    payload = {
        "run_id": "digest-1",
        "collection": "tech",
        "spec": SPEC,
        "script": SCRIPT,
        "strategy": "turns",
        "namespace": target.namespace,
        "task_queue": target.task_queue,
    }
    payload.update(overrides)
    return payload


async def test_dispatch_starts_the_workflow_in_the_requested_namespace(
    fake_client: FakeClient,
) -> None:
    result = await ActivityEnvironment().run(activities.dispatch_narration_activity, _payload())

    assert fake_client.namespace == settings.temporal_podcast_local_namespace
    assert len(fake_client.started) == 1
    started = fake_client.started[0]
    assert started["id"] == "narration-tech-digest-1"
    assert started["task_queue"] == settings.narration_local_task_queue
    assert started["config"].run_id == "digest-1"
    assert result == RENDER


async def test_dispatch_follows_the_backend_when_the_target_is_unpinned(
    fake_client: FakeClient,
) -> None:
    """An explicit Modal spec lands in the Modal namespace when unpinned."""
    payload = _payload(namespace="", task_queue="")
    del payload["namespace"]
    del payload["task_queue"]
    payload["spec"] = {
        **SPEC,
        "tts_backend": "qwen3_tts",
        "run_in_modal": True,
        "roles": [
            {"name": "anchor", "voice_id": "anchor_neutral_m"},
            {"name": "guest", "voice_id": "anchor_neutral_f"},
        ],
    }

    await ActivityEnvironment().run(activities.dispatch_narration_activity, payload)

    assert fake_client.namespace == settings.temporal_podcast_modal_namespace
    assert fake_client.started[0]["task_queue"] == settings.narration_modal_task_queue


async def test_dispatch_prefers_the_pinned_target_over_the_spec_backend(
    fake_client: FakeClient,
) -> None:
    """An explicit namespace and queue win, so an operator can override."""
    payload = _payload(
        namespace=settings.temporal_podcast_modal_namespace,
        task_queue=settings.narration_modal_task_queue,
    )

    await ActivityEnvironment().run(activities.dispatch_narration_activity, payload)

    assert fake_client.namespace == settings.temporal_podcast_modal_namespace
    assert fake_client.started[0]["task_queue"] == settings.narration_modal_task_queue


async def test_dispatch_reattaches_when_the_render_already_started(
    fake_client: FakeClient,
) -> None:
    """An activity retry must not pay for a second render."""
    fake_client.already_started = True

    result = await ActivityEnvironment().run(activities.dispatch_narration_activity, _payload())

    assert result == RENDER
    assert len(fake_client.handles) == 1


async def test_dispatch_rejects_a_payload_without_spec(fake_client: FakeClient) -> None:
    for payload in (_payload(spec=None), _payload(spec="not-a-dict"), _payload()):
        del payload["spec"]
        with pytest.raises(ApplicationError, match="spec"):
            await ActivityEnvironment().run(activities.dispatch_narration_activity, payload)

    assert fake_client.started == []


async def test_dispatch_rejects_a_malformed_spec(fake_client: FakeClient) -> None:
    """A bad cast is a configuration error: it must not be retried."""
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            activities.dispatch_narration_activity,
            _payload(spec={**SPEC, "roles": "not-a-list"}),
        )

    assert "invalid narration spec" in str(excinfo.value)
    assert "roles" in str(excinfo.value)
    assert fake_client.started == []


def test_the_workflow_id_is_derived_from_the_digest_run() -> None:
    assert activities._narration_workflow_id("tech", "digest-1") == "narration-tech-digest-1"
    assert activities._narration_workflow_id("ai", "digest-2") == "narration-ai-digest-2"


async def test_dispatch_cancels_a_stranded_render_when_the_activity_is_cancelled(
    fake_client: FakeClient,
) -> None:
    """Nothing would collect the audio, so the render must not be left running."""
    fake_client.block_result = True

    async def _run() -> dict[str, Any]:
        return await ActivityEnvironment().run(activities.dispatch_narration_activity, _payload())

    task = asyncio.ensure_future(_run())
    # Let the activity reach its polling loop before cancelling it.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    handle = fake_client.started_handle
    assert handle is not None
    assert handle.cancelled is True


async def test_dispatch_accepts_the_payload_the_digest_workflow_builds(
    fake_client: FakeClient,
) -> None:
    """Round trip: what ``DigestPipelineWorkflow`` builds must dispatch cleanly.

    The unit tests above hand-build the payload, so a drift between the
    workflow and the activity went unnoticed until a live run reported
    "dispatch_narration_activity needs a 'spec' payload". Build the payload
    with the real workflow helper instead.
    """
    from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig

    payload = DigestPipelineWorkflow._narration_payload(
        PipelineConfig(
            collection="tech",
            main_story_count=7,
            headline_count=9,
            prompt_template="ai",
            narration={
                "namespace": settings.temporal_podcast_local_namespace,
                "task_queue": settings.narration_local_task_queue,
                "spec": SPEC,
                "script": SCRIPT,
                "strategy": "turns",
            },
        ),
        "digest-round-trip",
    )

    result = await ActivityEnvironment().run(activities.dispatch_narration_activity, payload)

    assert fake_client.namespace == settings.temporal_podcast_local_namespace
    started = fake_client.started[0]
    assert started["id"] == "narration-tech-digest-round-trip"
    assert started["task_queue"] == settings.narration_local_task_queue
    config = started["config"]
    assert config.run_id == "digest-round-trip"
    assert config.spec == SPEC
    # The digest context has to reach the narration workflow: it generates the
    # script from that run's text when the caller carries no script.
    assert config.prompt_template == "ai"
    assert config.main_story_count == 7
    assert config.headline_count == 9
    assert result == RENDER
