"""Unit tests for the design_voice CLI entry point (Phase 7.9)."""

import json
import sys
from typing import Any

import pytest

from src.core.config import settings
from src.workers import design_voice
from src.workers.voice_workflows import VoiceCatalogWorkflow


class _FakeClient:
    """Stand-in for temporalio's Client: deliberately not a context manager.

    temporalio >= 1.32 gives ``Client`` no ``__aenter__``, so a CLI that wraps
    it in ``async with`` dies with a TypeError only when the real entry point
    runs. Workflow tests never touch this module, which is how the bug shipped.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_workflow(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record the call and return a plausible workflow result."""
        self.calls.append({"args": args, "kwargs": kwargs})
        return {"pending": ["anchor_neutral_m"], "results": []}


async def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> _FakeClient:
    """Run ``main()`` against a fake client and return it.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        *argv: Arguments after the program name.

    Returns:
        The fake client ``main()`` used.
    """
    client = _FakeClient()

    async def _build_client(namespace: str) -> _FakeClient:
        return client

    monkeypatch.setattr(design_voice, "build_client", _build_client)
    monkeypatch.setattr(design_voice, "_pending", lambda names, only: ["anchor_neutral_m"])
    monkeypatch.setattr(sys, "argv", ["design_voice", *argv])
    await design_voice.main()
    return client


async def test_cli_starts_the_workflow_without_entering_the_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The client is awaited, never used as ``async with`` (it cannot be)."""
    client = await _run_cli(monkeypatch, "--name", "anchor_neutral_m")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["args"][0] == VoiceCatalogWorkflow.run
    assert call["kwargs"]["task_queue"] == settings.voice_catalog_task_queue
    assert call["kwargs"]["id"].startswith("voice-catalog-")
    assert json.loads(capsys.readouterr().out)["pending"] == ["anchor_neutral_m"]


async def test_cli_only_asks_for_an_upload_when_the_flag_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --upload the workflow must not touch the Modal Volume."""
    client = await _run_cli(monkeypatch, "--name", "anchor_neutral_m")
    assert client.calls[0]["args"][1].upload is False

    client = await _run_cli(monkeypatch, "--name", "anchor_neutral_m", "--upload")
    assert client.calls[0]["args"][1].upload is True


async def test_cli_quits_before_connecting_when_nothing_is_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean catalog must not open a Temporal connection at all."""
    monkeypatch.setattr(design_voice, "_pending", lambda names, only: [])
    monkeypatch.setattr(sys, "argv", ["design_voice", "--all"])

    async def _build_client(namespace: str) -> _FakeClient:
        raise AssertionError("build_client must not be called")

    monkeypatch.setattr(design_voice, "build_client", _build_client)
    await design_voice.main()

    assert json.loads(capsys.readouterr().out)["pending"] == []
