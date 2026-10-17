"""Integration tests for the Phase 7.10 UrlSummarizeWorkflow."""

from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.workflows import UrlSummarizeConfig, UrlSummarizeWorkflow

ARTICLE = {
    "title": "OpenAI releases GPT-5",
    "link": "https://example.com/gpt5",
    "content": "OpenAI announced GPT-5 with a 12% improvement on MMLU.",
    "pub_date": "2026-10-02",
    "source": "example.com",
}
DIGEST = {
    "status": "ok",
    "digest": "# Top Stories\n\n- OpenAI releases GPT-5 (12% on MMLU)",
    "article_count": 1,
}
DELIVERY = {"status": "ok", "message_id": 42}
VERIFICATION_PASSED = {
    "status": "ok",
    "verdict": "valid",
    "score": 1.0,
    "threshold": 0.9,
    "passed": True,
    "backend": "local",
    "judge_model": "test-model",
    "reasons": [],
    "run_id": "url-test",
}
VERIFICATION_FAILED = {
    "status": "ok",
    "verdict": "invalid",
    "score": 0.5,
    "threshold": 0.9,
    "passed": False,
    "backend": "local",
    "judge_model": "test-model",
    "reasons": ["ungrounded figure: 999"],
    "run_id": "url-test",
}

SAVED_ARTICLES: list[dict[str, Any]] = []
RECORDED_RUNS: list[dict[str, Any]] = []
DELIVERED_CHANNELS: list[str] = []
FETCH_URL: list[str] = []


@activity.defn(name="fetch_url_activity")
async def stub_fetch_url(payload: dict[str, Any]) -> dict[str, Any]:
    FETCH_URL.append(str(payload.get("url")))
    if payload.get("url", "").startswith("https://broken"):
        from temporalio.exceptions import ApplicationError

        raise ApplicationError("URL extraction failed: no readable body", non_retryable=True)
    return ARTICLE


@activity.defn(name="save_article_activity")
async def stub_save_article(article: dict[str, Any], collection: str = "default") -> dict[str, Any]:
    SAVED_ARTICLES.append({"article": article, "collection": collection})
    return {"status": "ok", "saved": 1}


@activity.defn(name="generate_digest_activity")
async def stub_generate_digest(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["articles"], "the URL summary must summarize the extracted article"
    return DIGEST


@activity.defn(name="verify_digest_activity")
async def stub_verify_digest(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        VERIFICATION_FAILED if payload.get("digest", "").find("999") >= 0 else VERIFICATION_PASSED
    )


@activity.defn(name="deliver_telegram_message_activity")
async def stub_deliver_telegram(text: str, chat_id: str | None = None) -> dict:
    DELIVERED_CHANNELS.append("telegram")
    return DELIVERY


@activity.defn(name="deliver_email_activity")
async def stub_deliver_email(
    recipients: list[str], markdown_text: str, collection: str, audio_path: str | None = None
) -> dict:
    DELIVERED_CHANNELS.append("email")
    return {"status": "ok", "recipients": len(recipients)}


@activity.defn(name="deliver_whatsapp_message_activity")
async def stub_deliver_whatsapp(
    text: str, recipients: list[str] | None = None, group_id: str | None = None
) -> dict:
    DELIVERED_CHANNELS.append("whatsapp")
    return {"status": "ok", "recipients": len(recipients or [])}


@activity.defn(name="record_run_activity")
async def stub_record_run(run: dict[str, Any]) -> dict[str, Any]:
    RECORDED_RUNS.append(run)
    return {"status": "ok", "run_id": 1}


ALL_STUBS = [
    stub_fetch_url,
    stub_save_article,
    stub_generate_digest,
    stub_verify_digest,
    stub_deliver_telegram,
    stub_deliver_email,
    stub_deliver_whatsapp,
    stub_record_run,
]


def _reset() -> None:
    """Clear the per-test capture lists."""
    SAVED_ARTICLES.clear()
    RECORDED_RUNS.clear()
    DELIVERED_CHANNELS.clear()
    FETCH_URL.clear()


async def _run(config: UrlSummarizeConfig, *, workflow_id: str = "url-summary-test") -> dict:
    """Run UrlSummarizeWorkflow once against the time-skipping server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-url",
            workflows=[UrlSummarizeWorkflow],
            activities=ALL_STUBS,
        ):
            return await env.client.execute_workflow(
                UrlSummarizeWorkflow.run,
                config,
                id=workflow_id,
                task_queue="tq-url",
            )


async def test_url_summary_happy_path_delivers_to_telegram() -> None:
    """The article is fetched, summarized and delivered to Telegram."""
    _reset()
    config = UrlSummarizeConfig(url="https://example.com/gpt5", collection="tech")

    result = await _run(config)

    assert result["status"] == "ok"
    assert result["url"] == "https://example.com/gpt5"
    assert result["digest"] == DIGEST["digest"]
    assert FETCH_URL == ["https://example.com/gpt5"]
    assert DELIVERED_CHANNELS == ["telegram"]
    assert result["verification"] is None
    assert RECORDED_RUNS[-1]["status"] == "ok"
    assert RECORDED_RUNS[-1]["collection"] == "tech"


async def test_url_summary_without_delivery_records_only() -> None:
    """deliver=False generates the digest without touching any channel."""
    _reset()
    config = UrlSummarizeConfig(url="https://example.com/gpt5", deliver=False)

    result = await _run(config)

    assert result["status"] == "ok"
    assert result["delivery_status"] == []
    assert DELIVERED_CHANNELS == []


async def test_url_summary_store_persists_the_article() -> None:
    """store=True writes the extracted article to the articles table."""
    _reset()
    config = UrlSummarizeConfig(url="https://example.com/gpt5", store=True)

    await _run(config)

    assert len(SAVED_ARTICLES) == 1
    assert SAVED_ARTICLES[0]["article"]["link"] == ARTICLE["link"]
    assert SAVED_ARTICLES[0]["collection"] == "default"


async def test_url_summary_fetch_failure_is_recorded_and_fails() -> None:
    """An unreadable page fails the workflow after recording a failed run."""
    _reset()
    config = UrlSummarizeConfig(url="https://broken.example.com/a")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-url-fail",
            workflows=[UrlSummarizeWorkflow],
            activities=ALL_STUBS,
        ):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    UrlSummarizeWorkflow.run,
                    config,
                    id="url-summary-fail",
                    task_queue="tq-url-fail",
                )

    assert "URL summary failed" in str(excinfo.value.cause)
    assert DELIVERED_CHANNELS == []
    assert RECORDED_RUNS[-1]["status"] == "failed"
    assert "no readable body" in (RECORDED_RUNS[-1]["error"] or "")


async def test_url_summary_verification_failure_blocks_delivery() -> None:
    """A rejected digest is recorded as verification_failed and never delivered."""
    _reset()
    config = UrlSummarizeConfig(
        url="https://example.com/gpt5",
        verify={"threshold": 0.9, "backend": "local", "host_reliability": {}},
        store=True,
    )

    # The verify stub fails the gate when the digest carries 999, so this run
    # needs a generate stub that produces exactly that digest.
    @activity.defn(name="generate_digest_activity")
    async def stub_generate_bad(payload: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        return {
            "status": "ok",
            "digest": "# Top Stories\n\n- Revenue reached 999 billion",
            "article_count": 1,
        }

    activities = [a for a in ALL_STUBS if a is not stub_generate_digest]
    activities.append(stub_generate_bad)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-url-verify",
            workflows=[UrlSummarizeWorkflow],
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    UrlSummarizeWorkflow.run,
                    config,
                    id="url-summary-verify",
                    task_queue="tq-url-verify",
                )

    assert "Digest verification failed" in str(excinfo.value.cause)
    assert DELIVERED_CHANNELS == []
    assert RECORDED_RUNS[-1]["status"] == "verification_failed"


async def test_url_summary_verification_pass_is_reported() -> None:
    """A passing gate is surfaced in the workflow result."""
    _reset()
    config = UrlSummarizeConfig(
        url="https://example.com/gpt5",
        verify={"threshold": 0.9, "backend": "local", "host_reliability": {}},
    )

    result = await _run(config)

    assert result["verification"]["verdict"] == "valid"
    assert DELIVERED_CHANNELS == ["telegram"]
