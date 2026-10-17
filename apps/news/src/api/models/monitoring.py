"""Pydantic models for the system monitor checks and reports."""

from pydantic import BaseModel, Field

# Allowed CheckResult.status values. 'info' is used for passive notifications
# (e.g. a digest run completing) that are never treated as a problem.
CHECK_STATUSES = {"ok", "warning", "error", "skipped", "info"}


class CheckResult(BaseModel):
    """Outcome of a single system-monitor check."""

    check: str = Field(description="Name of the check (e.g. 'edge_tts').")
    status: str = Field(description="Outcome: 'ok', 'warning', 'error', 'skipped', or 'info'.")
    detail: str = Field(default="", description="Human-readable details or an empty string.")


class MonitorReport(BaseModel):
    """Aggregated result of running every registered check once."""

    ts: str = Field(description="ISO timestamp of the report.")
    checks: list[CheckResult] = Field(default_factory=list, description="Per-check outcomes.")
    alert: list[CheckResult] = Field(
        default_factory=list,
        description="Subset of checks that triggered a notification.",
    )


class MonitorRunConfig(BaseModel):
    """Options passed to SystemMonitorWorkflow from the schedule."""

    daily_summary: bool = Field(
        default=True,
        description="Send a full health summary once per UTC day.",
    )
    alert_on_run: bool = Field(
        default=False,
        description="Notify as soon as a digest run completes (info event).",
    )
