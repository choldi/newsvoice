"""End-to-end check that the observability interceptors compose on a worker.

Runs a tiny workflow through a real ``WorkflowEnvironment`` with the same
interceptors ``build_client`` installs, and asserts the correlation id set on
the client reaches the activity (and that workflow spans are recorded).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.core import context
from src.core.tracing import clear_spans, get_inmem_spans, get_tracer
from src.workers.interceptors import CorrelationInterceptor

SEEN_CORRELATION: list[str | None] = []


@activity.defn
async def echo_activity(value: str) -> str:
    """Record the correlation id visible to the activity."""
    SEEN_CORRELATION.append(context.get_correlation_id())
    return value


@workflow.defn
class EchoWorkflow:
    """Trivial workflow that calls one activity."""

    @workflow.run
    async def run(self, value: str) -> str:
        """Echo the value through an activity."""
        return await workflow.execute_activity(
            echo_activity,
            value,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_correlation_and_trace_propagate_to_activity() -> None:
    get_tracer()  # install the local in-memory provider first
    clear_spans()
    SEEN_CORRELATION.clear()
    context.set_correlation_id("e2e-correlation")
    interceptors = [
        TracingInterceptor(always_create_workflow_spans=True),
        CorrelationInterceptor(),
    ]
    try:
        async with await WorkflowEnvironment.start_time_skipping(interceptors=interceptors) as env:
            async with Worker(
                env.client,
                task_queue="obs-test-queue",
                workflows=[EchoWorkflow],
                activities=[echo_activity],
            ):
                result = await env.client.execute_workflow(
                    EchoWorkflow.run,
                    "hello",
                    id="obs-e2e",
                    task_queue="obs-test-queue",
                )
    finally:
        context.clear_correlation_id()

    assert result == "hello"
    assert SEEN_CORRELATION == ["e2e-correlation"]
    spans = get_inmem_spans()
    assert any("EchoWorkflow" in (s.get("name") or "") for s in spans)
