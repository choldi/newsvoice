"""Integration tests for SystemMonitorWorkflow via WorkflowEnvironment."""

from datetime import UTC, datetime

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.api.models.monitoring import MonitorRunConfig
from src.workers.workflows import SystemMonitorWorkflow

REPORT_OK = {
    "ts": "2026-09-21T10:00:00+00:00",
    "checks": [{"check": "edge_tts", "status": "ok", "detail": ""}],
    "alert": [],
}

REPORT_ALERT = {
    "ts": "2026-09-21T10:00:00+00:00",
    "checks": [
        {"check": "services", "status": "warning", "detail": "api down"},
        {"check": "edge_tts", "status": "ok", "detail": ""},
    ],
    "alert": [{"check": "services", "status": "warning", "detail": "api down"}],
}


#: Shared fixture state mutated by the per-test setup.
SEEN: dict = {}


@activity.defn(name="run_monitor_checks_activity")
async def stub_run_task(config: dict) -> dict:
    return SEEN["run_config"]


@activity.defn(name="send_monitor_alert_activity")
async def stub_send(text: str) -> dict:
    SEEN["sent"].append(text)
    return {"status": "ok"}


@activity.defn(name="get_monitor_state_activity")
async def stub_get(key: str) -> str | None:
    return SEEN["state"].get(key, SEEN["default_state"])


@activity.defn(name="set_monitor_state_activity")
async def stub_set(key: str, value: str) -> None:
    SEEN["state"][key] = value


def _reset_seen(run_config: dict, state: dict[str, str] | None = None) -> None:
    SEEN.clear()
    SEEN.update(
        {
            "sent": [],
            "run_config": run_config,
            "state": dict(state or {}),
            "default_state": None,
        }
    )


async def test_system_monitor_alerts_on_error() -> None:
    _reset_seen(REPORT_ALERT)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[SystemMonitorWorkflow],
            activities=[stub_run_task, stub_send],
        ):
            result = await env.client.execute_workflow(
                SystemMonitorWorkflow.run,
                MonitorRunConfig(daily_summary=False, alert_on_run=False),
                id="sysmon-alert",
                task_queue="tq",
            )

    assert result == {"status": "ok", "checks": 2, "alerts": 1, "summary_sent": False}
    assert len(SEEN["sent"]) == 1
    assert "News-engine alert" in SEEN["sent"][0]
    assert "api down" in SEEN["sent"][0]


async def test_system_monitor_sends_daily_summary_once() -> None:
    _reset_seen(REPORT_OK)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[SystemMonitorWorkflow],
            activities=[stub_run_task, stub_send, stub_get, stub_set],
        ):
            result = await env.client.execute_workflow(
                SystemMonitorWorkflow.run,
                MonitorRunConfig(daily_summary=True, alert_on_run=False),
                id="sysmon-summary",
                task_queue="tq",
            )

    assert result["summary_sent"] is True
    assert len(SEEN["sent"]) == 1
    assert "Daily news-engine health" in SEEN["sent"][0]
    assert "edge_tts" in SEEN["sent"][0]
    assert "monitor:last_summary_day" in SEEN["state"]


async def test_system_monitor_skips_duplicate_daily_summary() -> None:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _reset_seen(REPORT_OK, {"monitor:last_summary_day": today})

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[SystemMonitorWorkflow],
            activities=[stub_run_task, stub_send, stub_get, stub_set],
        ):
            result = await env.client.execute_workflow(
                SystemMonitorWorkflow.run,
                MonitorRunConfig(daily_summary=True, alert_on_run=False),
                id="sysmon-repeat",
                task_queue="tq",
            )

    assert result["summary_sent"] is False
    assert SEEN["sent"] == []


async def test_system_monitor_tolerates_missing_checks() -> None:
    """A report without a checks list still completes without alerting."""
    _reset_seen({"ts": "2026-09-21T10:00:00+00:00", "alert": []})

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[SystemMonitorWorkflow],
            activities=[stub_run_task, stub_send],
        ):
            result = await env.client.execute_workflow(
                SystemMonitorWorkflow.run,
                MonitorRunConfig(daily_summary=False, alert_on_run=False),
                id="sysmon-shape",
                task_queue="tq",
            )

    assert result == {"status": "ok", "checks": 0, "alerts": 0, "summary_sent": False}
    assert SEEN["sent"] == []
