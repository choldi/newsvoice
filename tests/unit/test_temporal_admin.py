"""Unit tests for the Temporal schedule admin service."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import RPCError, WorkflowExecutionStatus
from temporalio.service import RPCStatusCode

from src.api.services.temporal_admin import TemporalAdminService
from src.core.collections import CollectionConfig
from src.core.config import settings
from src.workers.schedule import pipeline_config_for, url_summarize_config_for


class FakeHandle:
    """Minimal ScheduleHandle stand-in."""

    def __init__(self, schedule_id: str, not_found: bool = False) -> None:
        self.id = schedule_id
        self._not_found = not_found
        self.updated = 0
        self.deleted = False

    async def describe(self) -> None:
        if self._not_found:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def update(self, schedule: object) -> None:
        self.updated += 1

    async def delete(self) -> None:
        self.deleted = True


class FakeWorkflow:
    """Minimal WorkflowExecution stand-in from Temporal visibility."""

    _STATUS = {
        "running": WorkflowExecutionStatus.RUNNING,
        "completed": WorkflowExecutionStatus.COMPLETED,
        "failed": WorkflowExecutionStatus.FAILED,
        "terminated": WorkflowExecutionStatus.TERMINATED,
    }

    def __init__(
        self,
        workflow_id: str,
        status: str,
        start_time: datetime,
        close_time: datetime | None,
    ) -> None:
        self.id = workflow_id
        self.status = self._STATUS[status]
        self.start_time = start_time
        self.close_time = close_time


class FakeWorkflowHandle:
    """Minimal WorkflowHandle stand-in supporting terminate/result."""

    def __init__(
        self, workflow_id: str, raise_on_result: bool = False, error: str = "boom"
    ) -> None:
        self.workflow_id = workflow_id
        self.terminated_reason: str | None = None
        self.result_called = False
        self._raise_on_result = raise_on_result
        self._error = error

    async def terminate(self, reason: str = "") -> None:
        self.terminated_reason = reason

    async def result(self) -> None:
        self.result_called = True
        if self._raise_on_result:
            raise RuntimeError(self._error)


class FakeClient:
    """Fake Temporal client supporting schedules, workflows and visibility."""

    def __init__(
        self,
        existing: list[str] | None = None,
        executions: list[FakeWorkflow] | None = None,
    ) -> None:
        self.schedules: dict[str, FakeHandle] = {
            schedule_id: FakeHandle(schedule_id) for schedule_id in (existing or [])
        }
        self.created: list[str] = []
        self.executions = executions or []
        self.started: list[str] = []
        self.started_kwargs: list[dict] = []
        self.workflow_handles: dict[str, FakeWorkflowHandle] = {}

    async def list_schedules(self):  # type: ignore[override]
        # Mirrors the real SDK: list_schedules() is a coroutine that must be
        # awaited and then yields handles via an async iterator.
        async def _iter():
            for handle in list(self.schedules.values()):
                yield handle

        return _iter()

    def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        if schedule_id not in self.schedules:
            return FakeHandle(schedule_id, not_found=True)
        return self.schedules[schedule_id]

    async def create_schedule(self, schedule_id: str, schedule: object) -> FakeHandle:
        handle = FakeHandle(schedule_id)
        self.schedules[schedule_id] = handle
        self.created.append(schedule_id)
        return handle

    async def start_workflow(self, *args: object, **kwargs: object) -> FakeWorkflowHandle:
        workflow_id = str(kwargs.get("id") or args[0])
        self.started.append(workflow_id)
        self.started_kwargs.append(kwargs)
        return FakeWorkflowHandle(workflow_id)

    async def list_workflows(self, query: str = "", limit: int = 10):  # type: ignore[override]
        del query
        for entry in self.executions[:limit]:
            yield entry

    def get_workflow_handle(self, workflow_id: str, run_id: str = "") -> FakeWorkflowHandle:
        del run_id
        if workflow_id not in self.workflow_handles:
            self.workflow_handles[workflow_id] = FakeWorkflowHandle(workflow_id)
        return self.workflow_handles[workflow_id]


def _tech(name: str = "tech") -> CollectionConfig:
    return CollectionConfig(
        name=name,
        time="08:00",
        feed_urls=["https://techcrunch.com/feed/"],
        podcast_name="Tech Daily",
        enabled=True,
    )


@pytest.mark.asyncio
async def test_pipeline_config_propagates_podcast_name() -> None:
    """pipeline_config_for carries podcast_name into the workflow config."""
    config = pipeline_config_for(_tech(), tts_flag=None)
    assert config.podcast_name == "Tech Daily"
    assert config.collection == "tech"


@pytest.mark.asyncio
async def test_resync_creates_new_and_removes_stale() -> None:
    """New collections get schedules; stale ones are deleted."""
    fake = FakeClient(existing=["daily-digest-removed"])
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    result = await service.resync_schedules([_tech(), _tech("finance")])

    assert "daily-digest-tech" in result["created_or_updated"]
    assert "daily-digest-finance" in result["created_or_updated"]
    assert result["removed"] == ["daily-digest-removed"]
    assert fake.schedules["daily-digest-tech"].updated == 0  # created, not updated


@pytest.mark.asyncio
async def test_resync_updates_existing_and_skips_disabled() -> None:
    """Existing schedules are updated; disabled collections are not scheduled."""
    fake = FakeClient(existing=["daily-digest-tech"])
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    enabled = _tech("tech").model_copy(update={"time": "12:30", "enabled": True})
    other = _tech("health").model_copy(update={"enabled": False})

    await service.resync_schedules([enabled, other])

    assert fake.schedules["daily-digest-tech"].updated == 1
    assert "daily-digest-health" not in fake.schedules


@pytest.mark.asyncio
async def test_uses_build_client_when_no_client_given() -> None:
    """Without an injected client, the service connects via build_client."""
    fake = FakeClient()
    with patch(
        "src.api.services.temporal_admin.build_client",
        new_callable=AsyncMock,
        return_value=fake,
    ) as mock_build:
        service = TemporalAdminService()
        await service.resync_schedules([_tech()])
        mock_build.assert_awaited_once()
    assert "daily-digest-tech" in fake.created


@pytest.mark.asyncio
async def test_trigger_collection_starts_fire_and_forget() -> None:
    """trigger_collection starts the workflow without waiting for completion."""
    fake = FakeClient()
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    workflow_id = await service.trigger_collection(_tech())

    assert workflow_id.startswith("digest-pipeline-tech-")
    assert fake.started == [workflow_id]


@pytest.mark.asyncio
async def test_is_collection_running() -> None:
    """A running digest workflow for the collection is detected via visibility."""
    now = datetime.now(UTC)
    fake = FakeClient(
        executions=[
            FakeWorkflow("digest-pipeline-tech-20260101000000", "running", now, None),
        ]
    )
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    assert await service.is_collection_running("tech") is True
    assert await service.is_collection_running("health") is False


@pytest.mark.asyncio
async def test_list_executions_maps_visibility() -> None:
    """list_executions normalizes status/dates coming from Temporal visibility."""
    now = datetime.now(UTC)
    fake = FakeClient(
        executions=[
            FakeWorkflow("digest-pipeline-tech-20260101000000", "completed", now, now),
            FakeWorkflow("digest-pipeline-health-20260101000000", "running", now, None),
        ]
    )
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    result = await service.list_executions()

    assert len(result) == 2
    assert result[0]["status"] == "completed"
    assert result[0]["close_time"] == now
    assert result[1]["status"] == "running"
    assert result[1]["close_time"] is None


@pytest.mark.asyncio
async def test_terminate_workflow_calls_handle() -> None:
    """terminate_workflow terminates the workflow handle with a reason."""
    fake = FakeClient()
    handle = FakeWorkflowHandle("digest-pipeline-tech-20260101000000")
    fake.workflow_handles["digest-pipeline-tech-20260101000000"] = handle
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    await service.terminate_workflow("digest-pipeline-tech-20260101000000", reason="GUI")

    assert handle.terminated_reason == "GUI"


@pytest.mark.asyncio
async def test_execution_error_skips_running_workflow() -> None:
    """A running workflow yields no error without blocking on result()."""
    now = datetime.now(UTC)
    fake = FakeClient(
        executions=[
            FakeWorkflow("digest-pipeline-tech-20260101000000", "running", now, None),
        ]
    )
    handle = FakeWorkflowHandle("digest-pipeline-tech-20260101000000")
    fake.workflow_handles["digest-pipeline-tech-20260101000000"] = handle
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    error = await service.execution_error("digest-pipeline-tech-20260101000000")

    assert error is None
    assert handle.result_called is False


@pytest.mark.asyncio
async def test_execution_error_returned_on_failure() -> None:
    """A failed workflow surfaces its error via handle.result()."""
    fake = FakeClient()
    handle = FakeWorkflowHandle(
        "digest-pipeline-tech-20260101000000", raise_on_result=True, error="timeout"
    )
    fake.workflow_handles["digest-pipeline-tech-20260101000000"] = handle
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    error = await service.execution_error("digest-pipeline-tech-20260101000000")

    assert error == "timeout"


@pytest.mark.asyncio
async def test_execution_error_none_on_success() -> None:
    """A completed workflow yields no error from execution_error."""
    fake = FakeClient()
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    error = await service.execution_error("digest-pipeline-tech-20260101000000")

    assert error is None


# ---------------------------------------------------------------------------
# Phase 7.10: single-URL summary
# ---------------------------------------------------------------------------


def _url_collection(**overrides: object) -> CollectionConfig:
    """Build a collection exercising every option a URL summary reads."""
    fields: dict[str, object] = {
        "name": "tech",
        "time": "08:00",
        "feed_urls": ["https://techcrunch.com/feed/"],
        "language": "en",
        "prompt_template": "default",
        "delivery_channels": ["telegram", "email"],
        "email_recipients": ["me@example.com"],
        "main_story_count": 7,
        "headline_count": 3,
    }
    fields.update(overrides)
    return CollectionConfig(**fields)  # type: ignore[arg-type]


def test_url_summarize_config_resolves_collection_options() -> None:
    """url_summarize_config_for carries prompt, counts and destinations."""
    config = url_summarize_config_for(
        _url_collection(), url="https://example.com/a", store=True, deliver=False
    )

    assert config.url == "https://example.com/a"
    assert config.collection == "tech"
    assert config.store is True
    assert config.deliver is False
    assert config.language == "en"
    assert config.main_story_count == 7
    assert config.headline_count == 3
    assert config.delivery_channels == ["telegram", "email"]
    assert config.email_recipients == ["me@example.com"]


def test_url_summarize_config_verification_is_opt_in() -> None:
    """Without llm_verify the URL summary runs without the Phase 7.13 gate."""
    config = url_summarize_config_for(
        _url_collection(), url="https://example.com/a", store=False, deliver=True
    )
    assert config.verify is None

    verified = url_summarize_config_for(
        _url_collection(llm_verify=True),
        url="https://example.com/a",
        store=False,
        deliver=True,
    )
    assert verified.verify is not None
    assert verified.verify["enabled"] is True
    assert verified.verify["backend"]


@pytest.mark.asyncio
async def test_start_url_summarize_starts_workflow() -> None:
    """start_url_summarize starts UrlSummarizeWorkflow with a unique id."""
    fake = FakeClient()
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    workflow_id = await service.start_url_summarize(
        _url_collection(), url="https://example.com/a", store=False, deliver=True
    )

    assert workflow_id.startswith("url-summary-tech-")
    assert fake.started == [workflow_id]


@pytest.mark.asyncio
async def test_start_document_tts_uses_the_dedicated_queue() -> None:
    """The document workflow is scheduled on the doc-tts queue, not the digest one."""
    from src.api.services.temporal_admin import new_document_run_id
    from src.workers.doc_tts_workflows import DocTTSConfig

    fake = FakeClient()
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]
    workflow_id = new_document_run_id()

    returned = await service.start_document_tts(
        DocTTSConfig(source_path="/data/doc_tts/uploads/x.pdf", filename="x.pdf"),
        workflow_id,
    )

    assert returned == workflow_id
    assert fake.started == [workflow_id]
    assert fake.started_kwargs[-1]["task_queue"] == settings.doc_tts_task_queue


def test_new_document_run_id_is_unique_and_prefixed() -> None:
    """Ids are unique (uploads and job rows key on them) and recognizable."""
    from src.api.services.temporal_admin import new_document_run_id

    ids = {new_document_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("doc-tts-") for i in ids)
