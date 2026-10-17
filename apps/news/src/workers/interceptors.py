"""Temporal interceptors that carry the correlation id across the boundary.

The OpenTelemetry ``TracingInterceptor`` handles spans and W3C trace-context
propagation. This module complements it with the *business* correlation id so
that a digest run can be followed from the trigger that started it through the
workflow and every activity.

The correlation id rides on a Temporal header of its own, which keeps the
behaviour deterministic: headers are part of the workflow input and are read on
every replay, unlike a contextvar that would have to be generated in workflow
code. A workflow with no incoming correlation id (for example one started by a
Temporal schedule) falls back to its own ``workflow_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from temporalio import client as _client
from temporalio import worker as _worker
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadConverter

from src.core import context

#: Temporal header key holding the correlation id.
CORRELATION_HEADER = "news-engine-correlation-id"

_CONVERTER = PayloadConverter.default


def encode_correlation(correlation_id: str) -> Payload:
    """Encode a correlation id as a Temporal header payload.

    Args:
        correlation_id: The correlation id to encode.

    Returns:
        A single header payload carrying the id.
    """
    return _CONVERTER.to_payloads([correlation_id])[0]


def decode_correlation(headers: Mapping[str, Payload]) -> str | None:
    """Read the correlation id from a header mapping if present.

    Args:
        headers: The Temporal headers attached to a workflow/activity task.

    Returns:
        The decoded correlation id, or ``None`` when absent or undecodable.
    """
    payload = headers.get(CORRELATION_HEADER)
    if payload is None:
        return None
    try:
        value = _CONVERTER.from_payloads([payload])[0]
    except Exception:
        return None
    return str(value) if value is not None else None


class CorrelationInterceptor(_client.Interceptor, _worker.Interceptor):
    """Propagate the correlation id from the client into workflows/activities."""

    def intercept_client(self, next: _client.OutboundInterceptor) -> _client.OutboundInterceptor:
        """Wrap the client outbound interceptor."""
        return _CorrelationClientOutbound(next)

    def intercept_activity(
        self, next: _worker.ActivityInboundInterceptor
    ) -> _worker.ActivityInboundInterceptor:
        """Wrap the activity inbound interceptor."""
        return _CorrelationActivityInbound(next)

    def workflow_interceptor_class(
        self, input: _worker.WorkflowInterceptorClassInput
    ) -> type[_CorrelationWorkflowInbound]:
        """Return the workflow inbound interceptor class."""
        return _CorrelationWorkflowInbound


class _CorrelationClientOutbound(_client.OutboundInterceptor):
    async def start_workflow(self, input: _client.StartWorkflowInput) -> Any:
        """Inject the current (or a fresh) correlation id into the start call."""
        if CORRELATION_HEADER not in input.headers:
            cid = context.get_correlation_id() or context.generate_correlation_id()
            context.set_correlation_id(cid)
            input.headers = {**input.headers, CORRELATION_HEADER: encode_correlation(cid)}
        else:
            existing = decode_correlation(input.headers)
            if existing:
                context.set_correlation_id(existing)
        return await self.next.start_workflow(input)


class _CorrelationActivityInbound(_worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: _worker.ExecuteActivityInput) -> Any:
        """Bind the correlation id from the task headers before running."""
        cid = decode_correlation(input.headers)
        if not cid:
            info = _activity_info_or_none()
            cid = getattr(info, "workflow_id", None) if info else None
        token = context.correlation_id_var.set(cid) if cid else None
        try:
            return await self.next.execute_activity(input)
        finally:
            if token is not None:
                context.correlation_id_var.reset(token)


class _CorrelationWorkflowInbound(_worker.WorkflowInboundInterceptor):
    def __init__(self, next: _worker.WorkflowInboundInterceptor) -> None:
        """Store the correlation id so the outbound side can forward it."""
        super().__init__(next)
        self.correlation_id: str | None = None

    def init(self, outbound: _worker.WorkflowOutboundInterceptor) -> None:
        """Wrap the outbound interceptor to stamp every activity start."""
        super().init(_CorrelationWorkflowOutbound(outbound, self))

    async def execute_workflow(self, input: _worker.ExecuteWorkflowInput) -> Any:
        """Bind the correlation id (header or workflow id) for the run."""
        cid = decode_correlation(input.headers)
        if not cid:
            from temporalio import workflow

            cid = workflow.info().workflow_id
        self.correlation_id = cid
        context.set_correlation_id(cid)
        return await super().execute_workflow(input)


class _CorrelationWorkflowOutbound(_worker.WorkflowOutboundInterceptor):
    def __init__(
        self,
        next: _worker.WorkflowOutboundInterceptor,
        root: _CorrelationWorkflowInbound,
    ) -> None:
        """Keep a reference to the inbound interceptor holding the id."""
        super().__init__(next)
        self._root = root

    def start_activity(self, input: _worker.StartActivityInput) -> Any:
        """Forward the correlation id to every activity the workflow starts."""
        cid = self._root.correlation_id
        if cid and CORRELATION_HEADER not in input.headers:
            input.headers = {**input.headers, CORRELATION_HEADER: encode_correlation(cid)}
        return super().start_activity(input)


def _activity_info_or_none() -> Any:
    """Return ``activity.info()`` when called from an activity worker context."""
    try:
        from temporalio import activity

        return activity.info()
    except Exception:
        return None
