"""Integration tests for the DigestPipelineWorkflow using the Temporal test env."""

import inspect

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers import workflows as workflows_module
from src.workers.workflows import (
    CollectionsMonitorWorkflow,
    DigestPipelineWorkflow,
    PipelineConfig,
)

ARTICLE_A = {
    "title": "OpenAI releases GPT-5",
    "link": "https://example.com/gpt5-a",
    "content": "OpenAI announced GPT-5.",
    "source": "TechCrunch",
}
ARTICLE_B = {
    "title": "Apple unveils iPhone 16",
    "link": "https://example.com/iphone16",
    "content": "Apple revealed the iPhone 16.",
    "source": "MacRumors",
}
CLUSTER = {
    "cluster_id": 1,
    "fingerprint": {
        "entity": "OpenAI",
        "action": "releases GPT-5",
        "title_hash": "abc123",
    },
    "articles": [ARTICLE_A],
    "is_material_update": True,
}
DIGEST = {
    "status": "ok",
    "digest": "# Top Stories\n\n- OpenAI releases GPT-5",
    "article_count": 2,
}
AUDIO = {
    "status": "ok",
    "audio_path": "/tmp/opencode/digest.wav",
    "duration_estimate": 5.0,
    "cached": False,
    "voice": "es-mx-male",
    "char_count": 100,
}
DELIVERY = {"status": "ok", "message_id": 42}


@activity.defn(name="fetch_rss_activity")
async def stub_fetch_rss(feed_urls: list[str]) -> list[dict]:
    return [ARTICLE_A, ARTICLE_B]


@activity.defn(name="filter_new_activity")
async def stub_filter_new(articles: list[dict], collection: str = "default") -> list[dict]:
    return articles


@activity.defn(name="cluster_activity")
async def stub_cluster(articles: list[dict], collection: str = "default") -> list[dict]:
    return [CLUSTER]


@activity.defn(name="generate_digest_activity")
async def stub_generate_digest(payload: dict) -> dict:
    return DIGEST


@activity.defn(name="generate_tts_activity")
async def stub_generate_tts(digest: dict) -> dict:
    return AUDIO


COMPRESSED_AUDIO = AUDIO["audio_path"].rsplit(".", 1)[0] + ".m4a"


@activity.defn(name="compress_audio_activity")
async def stub_compress_audio(audio_path: str) -> dict:
    return {"audio_path": audio_path.rsplit(".", 1)[0] + ".m4a"}


@activity.defn(name="deliver_telegram_message_activity")
async def stub_deliver_message(text: str, chat_id: str | None = None) -> dict:
    return DELIVERY


AUDIO_DELIVERED: list[str] = []
AUDIO_VERIFY_PASSED = True


@activity.defn(name="deliver_telegram_audio_activity")
async def stub_deliver_audio(
    audio_path: str, caption: str | None = None, chat_id: str | None = None
) -> dict:
    AUDIO_DELIVERED.append(audio_path)
    return DELIVERY


@activity.defn(name="verify_tts_audio_activity")
async def stub_verify_tts_audio(payload: dict) -> dict:
    verdict = "valid" if AUDIO_VERIFY_PASSED else "invalid"
    return {
        "passed": AUDIO_VERIFY_PASSED,
        "verdict": verdict,
        "score": 0.05 if AUDIO_VERIFY_PASSED else 0.9,
        "threshold": 0.3,
        "audio_path": payload.get("audio_path"),
        "reason": None if AUDIO_VERIFY_PASSED else "WER 0.9 above threshold 0.3",
    }


@activity.defn(name="deliver_email_activity")
async def stub_deliver_email(
    recipients: list[str],
    markdown_text: str,
    collection: str,
    audio_path: str | None = None,
) -> dict:
    return {"status": "ok", "recipients": len(recipients)}


@activity.defn(name="deliver_whatsapp_message_activity")
async def stub_deliver_whatsapp_message(
    text: str, recipients: list[str] | None = None, group_id: str | None = None
) -> dict:
    return {"status": "ok", "recipients": len(recipients or [])}


@activity.defn(name="deliver_whatsapp_audio_activity")
async def stub_deliver_whatsapp_audio(
    audio_path: str,
    caption: str | None = None,
    recipients: list[str] | None = None,
    group_id: str | None = None,
) -> dict:
    return {"status": "ok", "recipients": len(recipients or [])}


@activity.defn(name="record_run_activity")
async def stub_record_run(run: dict) -> dict:
    return {"status": "ok", "run_id": 1}


LOGIC = {
    "status": "ok",
    "logic": "Se seleccionaron 5 historias por su impacto publico y novedad.",
}


@activity.defn(name="generate_logic_activity")
async def stub_generate_logic(payload: dict) -> dict:
    return LOGIC


VERIFICATION_PASSED = {
    "status": "ok",
    "verdict": "valid",
    "score": 1.0,
    "threshold": 0.9,
    "passed": True,
    "backend": "local",
    "judge_model": "test-model",
    "reasons": [],
    "run_id": "workflow-test",
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
    "run_id": "workflow-test",
}


@activity.defn(name="verify_digest_activity")
async def stub_verify_digest(payload: dict) -> dict:
    return VERIFICATION_PASSED


@activity.defn(name="verify_digest_activity")
async def stub_verify_digest_fail(payload: dict) -> dict:
    return VERIFICATION_FAILED


@activity.defn(name="deliver_logic_activity")
async def stub_deliver_logic(logic: str, delivery: dict, collection: str) -> dict:
    return {
        "status": "ok",
        "deliveries": [{"channel": "telegram", "status": "ok", "message_id": 5}],
    }


async def _run_workflow(config: PipelineConfig) -> dict:
    """Run DigestPipelineWorkflow once against the time-skipping server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                stub_deliver_message,
                stub_deliver_audio,
                stub_deliver_email,
                stub_deliver_whatsapp_message,
                stub_deliver_whatsapp_audio,
                stub_generate_logic,
                stub_deliver_logic,
                stub_verify_digest,
                stub_verify_tts_audio,
                stub_record_run,
            ],
        ):
            return await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                config,
                id="workflow-test",
                task_queue="tq",
            )


async def test_full_pipeline_happy_path() -> None:
    """Full pipeline fetches, clusters, summarizes, and delivers."""
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"], tts_enabled=True, deliver_audio=True
    )
    result = await _run_workflow(config)

    assert result["status"] == "ok"
    assert result["article_count"] == 2
    assert result["cluster_count"] == 1
    assert result["digest"] == DIGEST["digest"]
    assert len(result["delivery_status"]) == 2


async def test_audio_delivered_uses_compressed_m4a() -> None:
    """The compressed M4A path (not the raw WAV) reaches the audio delivery."""
    seen: dict[str, str] = {}

    @activity.defn(name="deliver_telegram_audio_activity")
    async def audio_recorder(
        audio_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> dict:
        seen["path"] = audio_path
        return DELIVERY

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                stub_deliver_message,
                audio_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                ),
                id="workflow-test-compressed",
                task_queue="tq",
            )

    assert seen["path"] == COMPRESSED_AUDIO


async def test_compression_failure_falls_back_to_wav() -> None:
    """When ffmpeg fails, the workflow still delivers the original WAV."""
    seen: dict[str, str] = {}

    @activity.defn(name="compress_audio_activity")
    async def failing_compress(audio_path: str) -> dict:
        from temporalio.exceptions import ApplicationError as TemporalError

        raise TemporalError("ffmpeg not found", non_retryable=True)

    @activity.defn(name="deliver_telegram_audio_activity")
    async def audio_recorder(
        audio_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> dict:
        seen["path"] = audio_path
        return DELIVERY

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                failing_compress,
                stub_deliver_message,
                audio_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                ),
                id="workflow-test-compress-fallback",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert seen["path"] == AUDIO["audio_path"]


async def test_podcast_behavior_passed_to_digest_activity() -> None:
    """podcast_behavior in PipelineConfig reaches the digest activity payload."""
    received: dict = {}

    @activity.defn(name="generate_digest_activity")
    async def recording_digest(payload: dict) -> dict:
        received["payload"] = payload
        return DIGEST

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                recording_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    podcast_behavior="eres un podcaster",
                ),
                id="workflow-test-behavior",
                task_queue="tq",
            )

    assert received["payload"].get("podcast_behavior") == "eres un podcaster"


async def test_language_passed_to_digest_activity() -> None:
    """language in PipelineConfig reaches the digest activity payload."""
    received: dict = {}

    @activity.defn(name="generate_digest_activity")
    async def recording_digest(payload: dict) -> dict:
        received["payload"] = payload
        return DIGEST

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                recording_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    language="es",
                ),
                id="workflow-test-language",
                task_queue="tq",
            )

    assert received["payload"].get("language") == "es"


async def test_language_and_voice_passed_to_tts_activity() -> None:
    """language and voice in PipelineConfig reach the generate_tts_activity payload."""
    received: dict = {}

    @activity.defn(name="generate_tts_activity")
    async def recording_tts(payload: dict) -> dict:
        received["payload"] = payload
        return AUDIO

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                recording_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=False,
                    language="en",
                    voice="en-gb-female",
                ),
                id="workflow-test-tts",
                task_queue="tq",
            )

    assert received["payload"].get("language") == "en"
    assert received["payload"].get("voice") == "en-gb-female"


async def test_no_new_articles_skips_digest() -> None:
    """When filtering returns nothing, the pipeline stops before the LLM steps."""
    async with await WorkflowEnvironment.start_time_skipping() as env:

        @activity.defn(name="filter_new_activity")
        async def noop_filter(articles: list[dict], collection: str = "default") -> list[dict]:
            return []

        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                noop_filter,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(feed_urls=["https://feed.example/rss"]),
                id="workflow-test-empty",
                task_queue="tq",
            )

    assert result["status"] == "no_new_articles"
    assert result["article_count"] == 0


async def test_tts_disabled_skips_audio() -> None:
    """With tts_enabled=False, no audio is generated or delivered."""
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"],
        tts_enabled=False,
        deliver_audio=True,
    )
    result = await _run_workflow(config)

    assert result["status"] == "ok"
    # Only the text delivery should be recorded
    assert len(result["delivery_status"]) == 1


async def test_run_is_recorded_for_happy_path() -> None:
    """record_run_activity receives the run summary after a successful pipeline."""
    recorded: dict = {}

    @activity.defn(name="record_run_activity")
    async def recording_run(run: dict) -> dict:
        recorded.update(run)
        return {"status": "ok", "run_id": 7}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                recording_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="recorded-col",
                ),
                id="workflow-test-recorded",
                task_queue="tq",
            )

    assert recorded["workflow_id"] == "workflow-test-recorded"
    assert recorded["collection"] == "recorded-col"
    assert recorded["status"] == "ok"
    assert recorded["article_count"] == 2
    assert recorded["cluster_count"] == 1
    assert recorded["digest_preview"] == DIGEST["digest"]
    assert recorded["audio_path"] is None
    assert recorded["error"] is None


async def test_run_is_recorded_when_no_new_articles() -> None:
    """record_run_activity receives a no_new_articles summary when filtering is empty."""
    recorded: dict = {}

    @activity.defn(name="filter_new_activity")
    async def noop_filter(articles: list[dict], collection: str = "default") -> list[dict]:
        return []

    @activity.defn(name="record_run_activity")
    async def recording_run(run: dict) -> dict:
        recorded.update(run)
        return {"status": "ok", "run_id": 8}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                noop_filter,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                recording_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    collection="empty-col",
                ),
                id="workflow-test-empty-recorded",
                task_queue="tq",
            )

    assert result["status"] == "no_new_articles"
    assert recorded["workflow_id"] == "workflow-test-empty-recorded"
    assert recorded["collection"] == "empty-col"
    assert recorded["status"] == "no_new_articles"
    assert recorded["article_count"] == 0


async def test_podcast_name_passed_to_digest_activity() -> None:
    """podcast_name in PipelineConfig reaches the digest activity payload."""
    received: dict = {}

    @activity.defn(name="generate_digest_activity")
    async def recording_digest(payload: dict) -> dict:
        received["payload"] = payload
        return DIGEST

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                recording_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    podcast_name="ByteCast",
                ),
                id="workflow-test-podcast-name",
                task_queue="tq",
            )

    assert received["payload"].get("podcast_name") == "ByteCast"


async def test_collection_passed_to_filter_and_cluster() -> None:
    """collection in PipelineConfig reaches the filter and cluster activities."""
    seen: dict[str, str | None] = {}

    @activity.defn(name="filter_new_activity")
    async def filter_recorder(articles: list[dict], collection: str = "default") -> list[dict]:
        seen["filter"] = collection
        return articles

    @activity.defn(name="cluster_activity")
    async def cluster_recorder(articles: list[dict], collection: str = "default") -> list[dict]:
        seen["cluster"] = collection
        return [CLUSTER]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                filter_recorder,
                cluster_recorder,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="tech",
                ),
                id="workflow-test-collection",
                task_queue="tq",
            )

    assert seen == {"filter": "tech", "cluster": "tech"}


async def test_prompt_template_and_collection_reach_digest() -> None:
    """prompt_template and collection in PipelineConfig reach the digest payload."""
    received: dict = {}

    @activity.defn(name="generate_digest_activity")
    async def recording_digest(payload: dict) -> dict:
        received["payload"] = payload
        return DIGEST

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                recording_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="finance",
                    prompt_template="finance",
                ),
                id="workflow-test-template",
                task_queue="tq",
            )

    assert received["payload"].get("prompt_template") == "finance"
    assert received["payload"].get("collection") == "finance"


async def test_chat_id_passed_to_delivery() -> None:
    """chat_id in PipelineConfig reaches the delivery activities."""
    seen: dict[str, object] = {}

    @activity.defn(name="deliver_telegram_message_activity")
    async def message_recorder(text: str, chat_id: str | None = None) -> dict:
        seen["message_chat"] = chat_id
        return DELIVERY

    @activity.defn(name="deliver_telegram_audio_activity")
    async def audio_recorder(
        audio_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> dict:
        seen["audio_chat"] = chat_id
        return DELIVERY

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                message_recorder,
                audio_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                    collection="health",
                    chat_id="-1001234567890",
                ),
                id="workflow-test-chat",
                task_queue="tq",
            )

    assert seen == {"message_chat": "-1001234567890", "audio_chat": "-1001234567890"}


async def test_email_recipients_passed_to_delivery() -> None:
    """email_recipients + audio in PipelineConfig reach the email activity."""
    seen: dict[str, object] = {}

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        seen["recipients"] = recipients
        seen["text"] = markdown_text
        seen["collection"] = collection
        seen["audio"] = audio_path
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                stub_deliver_message,
                stub_deliver_audio,
                email_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                    collection="tech",
                    email_recipients=["a@example.com", "b@example.com"],
                    delivery_channels=["telegram", "email"],
                ),
                id="workflow-test-email",
                task_queue="tq",
            )

    assert seen["recipients"] == ["a@example.com", "b@example.com"]
    assert seen["text"] == DIGEST["digest"]
    assert seen["collection"] == "tech"
    assert seen["audio"] == COMPRESSED_AUDIO
    assert any(d.get("recipients") == 2 for d in result["delivery_status"])


async def test_email_delivery_skipped_without_recipients() -> None:
    """With no email_recipients, the email activity is not invoked."""
    invites: list[str] = []

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        invites.append(collection)
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                email_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="tech",
                ),
                id="workflow-test-no-email",
                task_queue="tq",
            )

    assert invites == []


async def test_email_failure_does_not_fail_workflow() -> None:
    """A failing email delivery is appended as an error status, not fatal."""
    from temporalio.exceptions import ApplicationError as TemporalError

    @activity.defn(name="deliver_email_activity")
    async def failing_email(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        raise TemporalError("Email delivery failed: relay down", non_retryable=True)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                failing_email,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="tech",
                    email_recipients=["a@example.com"],
                    delivery_channels=["telegram", "email"],
                ),
                id="workflow-test-email-fail",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert any(
        d.get("status") == "error" and "relay down" in d.get("error", "")
        for d in result["delivery_status"]
    )
    assert any(d.get("status") == "ok" for d in result["delivery_status"])


async def test_email_channel_only_skips_telegram() -> None:
    """With only the email channel selected, Telegram activities never run."""
    telegram_calls = 0
    seen: dict[str, object] = {}

    @activity.defn(name="deliver_telegram_message_activity")
    async def message_recorder(text: str, chat_id: str | None = None) -> dict:
        nonlocal telegram_calls
        telegram_calls += 1
        return DELIVERY

    @activity.defn(name="deliver_telegram_audio_activity")
    async def audio_recorder(
        audio_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> dict:
        nonlocal telegram_calls
        telegram_calls += 1
        return DELIVERY

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        seen["audio"] = audio_path
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                message_recorder,
                audio_recorder,
                email_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                    collection="tech",
                    email_recipients=["a@example.com"],
                    delivery_channels=["email"],
                ),
                id="workflow-test-email-only",
                task_queue="tq",
            )

    assert telegram_calls == 0
    assert seen["audio"] == COMPRESSED_AUDIO


async def test_empty_delivery_channels_skips_all_deliveries() -> None:
    """An explicit empty channel list disables every delivery activity."""
    calls: list[str] = []

    @activity.defn(name="deliver_telegram_message_activity")
    async def message_recorder(text: str, chat_id: str | None = None) -> dict:
        calls.append("telegram")
        return DELIVERY

    @activity.defn(name="deliver_telegram_audio_activity")
    async def audio_recorder(
        audio_path: str, caption: str | None = None, chat_id: str | None = None
    ) -> dict:
        calls.append("telegram_audio")
        return DELIVERY

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        calls.append("email")
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                message_recorder,
                audio_recorder,
                email_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                    collection="tech",
                    email_recipients=["a@example.com"],
                    delivery_channels=[],
                ),
                id="workflow-test-no-delivery",
                task_queue="tq",
            )

    assert calls == []
    assert result["delivery_status"] == []


async def test_email_without_deliver_audio_has_no_attachment() -> None:
    """deliver_audio=False passes no audio path to the email activity."""
    seen: dict[str, object] = {}

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        seen["audio"] = audio_path
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                email_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=False,
                    collection="tech",
                    email_recipients=["a@example.com"],
                    delivery_channels=["telegram", "email"],
                ),
                id="workflow-test-email-no-audio",
                task_queue="tq",
            )

    assert seen["audio"] is None


async def test_telegram_default_skips_email() -> None:
    """Default channels (telegram) skip email even with recipients configured."""
    invites: list[str] = []

    @activity.defn(name="deliver_email_activity")
    async def email_recorder(
        recipients: list[str],
        markdown_text: str,
        collection: str,
        audio_path: str | None = None,
    ) -> dict:
        invites.append(collection)
        return {"status": "ok", "recipients": len(recipients)}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                email_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="tech",
                    email_recipients=["a@example.com"],
                ),
                id="workflow-test-default-channels",
                task_queue="tq",
            )

    assert invites == []


async def test_whatsapp_channel_delivers_text_and_audio() -> None:
    """With the whatsapp channel enabled, text and audio activities run."""
    seen: dict[str, object] = {}

    @activity.defn(name="deliver_whatsapp_message_activity")
    async def message_recorder(
        text: str, recipients: list[str] | None = None, group_id: str | None = None
    ) -> dict:
        seen["text"] = text
        seen["recipients"] = list(recipients or [])
        seen["group_id"] = group_id
        return {"status": "ok", "recipients": len(recipients or [])}

    @activity.defn(name="deliver_whatsapp_audio_activity")
    async def audio_recorder(
        audio_path: str,
        caption: str | None = None,
        recipients: list[str] | None = None,
        group_id: str | None = None,
    ) -> dict:
        seen["audio"] = audio_path
        return {"status": "ok", "recipients": len(recipients or [])}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                stub_deliver_message,
                stub_deliver_audio,
                message_recorder,
                audio_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=True,
                    deliver_audio=True,
                    collection="tech",
                    whatsapp_recipients=["+34600000001"],
                    whatsapp_group_id="1234567890",
                    delivery_channels=["telegram", "whatsapp"],
                ),
                id="workflow-test-whatsapp",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert seen["recipients"] == ["+34600000001"]
    assert seen["group_id"] == "1234567890"
    assert seen["text"] == DIGEST["digest"]
    assert seen["audio"] == COMPRESSED_AUDIO
    assert any(d.get("recipients") == 1 for d in result["delivery_status"])


async def test_whatsapp_skipped_without_recipients() -> None:
    """Without whatsapp_recipients, the whatsapp activities are not invoked."""
    invites: list[str] = []

    @activity.defn(name="deliver_whatsapp_message_activity")
    async def message_recorder(
        text: str, recipients: list[str] | None = None, group_id: str | None = None
    ) -> dict:
        invites.append("message")
        return {"status": "ok", "recipients": 0}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_deliver_message,
                stub_deliver_audio,
                message_recorder,
                stub_record_run,
            ],
        ):
            await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="tech",
                    delivery_channels=["telegram", "whatsapp"],
                ),
                id="workflow-test-no-whatsapp",
                task_queue="tq",
            )

    assert invites == []


COLLECTIONS_SIGNATURE = "deadbeefcafe1234"


async def test_monitor_workflow_syncs_when_collections_change() -> None:
    """A signature different from the stored one triggers a schedule resync."""
    synced: list[str] = []

    @activity.defn(name="read_collections_signature_activity")
    async def stub_read_signature() -> str:
        return COLLECTIONS_SIGNATURE

    @activity.defn(name="get_synced_signature_activity")
    async def stub_get_synced() -> str | None:
        return "previous-signature"

    @activity.defn(name="collections_changes_activity")
    async def stub_changes() -> dict:
        return {"added": [], "removed": []}

    @activity.defn(name="sync_collections_schedules_activity")
    async def stub_sync(signature: str) -> None:
        synced.append(signature)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[CollectionsMonitorWorkflow],
            activities=[
                stub_read_signature,
                stub_get_synced,
                stub_changes,
                stub_sync,
            ],
        ):
            result = await env.client.execute_workflow(
                CollectionsMonitorWorkflow.run,
                None,
                id="monitor-changed",
                task_queue="tq",
            )

    assert result["status"] == "synced"
    assert result["changed"] is True
    assert result["signature"] == COLLECTIONS_SIGNATURE[:12]
    assert synced == [COLLECTIONS_SIGNATURE]


async def test_logic_delivery_generates_and_delivers() -> None:
    """logic_delivery triggers the LLM logic call and delivers it separately."""
    seen: dict[str, object] = {}

    @activity.defn(name="generate_logic_activity")
    async def logic_recorder(payload: dict) -> dict:
        seen["payload"] = payload
        return LOGIC

    @activity.defn(name="deliver_logic_activity")
    async def deliver_recorder(logic: str, delivery: dict, collection: str) -> dict:
        seen["logic"] = logic
        seen["delivery"] = delivery
        seen["collection"] = collection
        return {
            "status": "ok",
            "deliveries": [{"channel": "email", "status": "ok", "recipients": 1}],
        }

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_deliver_message,
                logic_recorder,
                deliver_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="health",
                    logic_delivery={
                        "channels": ["email"],
                        "email_recipients": ["admin@example.com"],
                    },
                ),
                id="workflow-test-logic",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert seen["payload"]["digest"] == DIGEST["digest"]
    assert seen["payload"]["collection"] == "health"
    assert seen["payload"]["prompt_template"] == "default"
    assert seen["logic"] == LOGIC["logic"]
    assert seen["collection"] == "health"
    assert seen["delivery"]["email_recipients"] == ["admin@example.com"]
    assert result["delivery_status"][-1] == {
        "channel": "email",
        "status": "ok",
        "recipients": 1,
    }


async def test_logic_failure_recorded_not_fatal() -> None:
    """A failing selection-logic step is recorded without failing the run."""
    from temporalio.exceptions import ApplicationError as TemporalError

    @activity.defn(name="generate_logic_activity")
    async def failing_logic(payload: dict) -> dict:
        raise TemporalError("logic generation failed: llm down", non_retryable=True)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_deliver_message,
                failing_logic,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="health",
                    logic_delivery={"channels": ["telegram"]},
                ),
                id="workflow-test-logic-fail",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert any(
        d.get("status") == "error" and "llm down" in d.get("error", "")
        for d in result["delivery_status"]
    )


async def test_logic_skipped_without_logic_delivery() -> None:
    """Without logic_delivery, the selection-logic activities never run."""
    calls: list[str] = []

    @activity.defn(name="generate_logic_activity")
    async def logic_recorder(payload: dict) -> dict:
        calls.append("generate")
        return LOGIC

    @activity.defn(name="deliver_logic_activity")
    async def deliver_recorder(logic: str, delivery: dict, collection: str) -> dict:
        calls.append("deliver")
        return {"status": "ok", "deliveries": []}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_deliver_message,
                logic_recorder,
                deliver_recorder,
                stub_record_run,
            ],
        ):
            result = await env.client.execute_workflow(
                DigestPipelineWorkflow.run,
                PipelineConfig(
                    feed_urls=["https://feed.example/rss"],
                    tts_enabled=False,
                    collection="health",
                ),
                id="workflow-test-no-logic",
                task_queue="tq",
            )

    assert result["status"] == "ok"
    assert calls == []


async def test_monitor_workflow_skips_when_unchanged() -> None:
    """An identical signature short-circuits without calling the sync activity."""
    synced: list[str] = []

    @activity.defn(name="read_collections_signature_activity")
    async def stub_read_signature() -> str:
        return COLLECTIONS_SIGNATURE

    @activity.defn(name="get_synced_signature_activity")
    async def stub_get_synced() -> str | None:
        return COLLECTIONS_SIGNATURE

    @activity.defn(name="sync_collections_schedules_activity")
    async def stub_sync(signature: str) -> None:
        synced.append(signature)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[CollectionsMonitorWorkflow],
            activities=[
                stub_read_signature,
                stub_get_synced,
                stub_sync,
            ],
        ):
            result = await env.client.execute_workflow(
                CollectionsMonitorWorkflow.run,
                None,
                id="monitor-unchanged",
                task_queue="tq",
            )

    assert result["status"] == "unchanged"
    assert result["changed"] is False
    assert synced == []


async def test_monitor_workflow_skips_when_file_unavailable() -> None:
    """A missing/unparsable collections file leaves schedules untouched."""
    synced: list[str] = []

    @activity.defn(name="read_collections_signature_activity")
    async def stub_read_signature() -> str | None:
        return None

    @activity.defn(name="get_synced_signature_activity")
    async def stub_get_synced() -> str | None:
        return COLLECTIONS_SIGNATURE

    @activity.defn(name="sync_collections_schedules_activity")
    async def stub_sync(signature: str) -> None:
        synced.append(signature)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq",
            workflows=[CollectionsMonitorWorkflow],
            activities=[
                stub_read_signature,
                stub_get_synced,
                stub_sync,
            ],
        ):
            result = await env.client.execute_workflow(
                CollectionsMonitorWorkflow.run,
                None,
                id="monitor-skipped",
                task_queue="tq",
            )

    assert result["status"] == "skipped"
    assert result["changed"] is False
    assert synced == []


@activity.defn(name="generate_digest_activity")
async def stub_generate_digest_no_text(payload: dict) -> dict:
    """Return a digest result with no 'digest' key."""
    return {"status": "ok", "article_count": 1}


@activity.defn(name="generate_digest_activity")
async def stub_generate_digest_no_count(payload: dict) -> dict:
    """Return a digest result with no 'article_count' key."""
    return {"status": "ok", "digest": DIGEST["digest"]}


async def test_digest_result_validation_raises_application_errors() -> None:
    """An incomplete digest result must fail the workflow with a clear message.

    These checks used ``activity.ApplicationError``, a symbol that does not
    exist on ``temporalio.activity``, so an incomplete result crashed the
    workflow task with an ``AttributeError`` (retried forever) instead of
    failing non-retryably with the intended message.
    """
    config = PipelineConfig(feed_urls=["https://feed.example/rss"], tts_enabled=False)

    async def _run(bad_digest: object, expected: str) -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="tq-bad-digest",
                workflows=[DigestPipelineWorkflow],
                activities=[
                    stub_fetch_rss,
                    stub_filter_new,
                    stub_cluster,
                    bad_digest,
                    stub_record_run,
                ],
            ):
                with pytest.raises(WorkflowFailureError) as excinfo:
                    await env.client.execute_workflow(
                        DigestPipelineWorkflow.run,
                        config,
                        id="bad-digest",
                        task_queue="tq-bad-digest",
                    )
        assert expected in str(excinfo.value.cause), excinfo.value.cause

    await _run(stub_generate_digest_no_text, "generate_digest_activity returned no digest text")
    await _run(stub_generate_digest_no_count, "generate_digest_activity returned no article_count")


def test_workflow_never_references_a_missing_application_error_symbol() -> None:
    """Guard the other validation branches, which are unreachable by design.

    ``isinstance(articles, list)`` style checks can never fail while the
    activity declares ``result_type=list[...]``, because the payload converter
    rejects the wrong shape first (and the workflow task then retries). They are
    kept as defence in depth, but they cannot be covered end to end.
    """
    source = inspect.getsource(workflows_module)

    assert "activity.ApplicationError" not in source
    assert workflows_module.ApplicationError is ApplicationError


async def test_pipeline_with_verification_passes() -> None:
    """A collection with verify=True and a passing report delivers normally."""
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"],
        tts_enabled=True,
        deliver_audio=True,
        verify={
            "threshold": 0.9,
            "groundedness_threshold": 1.0,
            "backend": "local",
            "model": "test-model",
            "host_reliability": {},
        },
    )
    result = await _run_workflow(config)

    assert result["status"] == "ok"
    assert result["verification"] is not None
    assert result["verification"]["verdict"] == "valid"


async def test_pipeline_with_verification_failure_blocks_delivery() -> None:
    """A failed verification records the run and raises before any delivery."""
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"],
        tts_enabled=True,
        deliver_audio=True,
        verify={
            "threshold": 0.9,
            "groundedness_threshold": 1.0,
            "backend": "local",
            "model": "test-model",
            "host_reliability": {},
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-verify-fail",
            workflows=[DigestPipelineWorkflow],
            activities=[
                stub_fetch_rss,
                stub_filter_new,
                stub_cluster,
                stub_generate_digest,
                stub_generate_tts,
                stub_compress_audio,
                stub_deliver_message,
                stub_deliver_audio,
                stub_deliver_email,
                stub_deliver_whatsapp_message,
                stub_deliver_whatsapp_audio,
                stub_generate_logic,
                stub_deliver_logic,
                stub_verify_digest_fail,
                stub_record_run,
            ],
        ):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    DigestPipelineWorkflow.run,
                    config,
                    id="verify-fail-test",
                    task_queue="tq-verify-fail",
                )
    cause = str(excinfo.value.cause)
    assert "verification failed" in cause.lower()


# ---------------------------------------------------------------------------
# Phase 7.14: audio-only WER gate
# ---------------------------------------------------------------------------


async def test_audio_verification_failure_withholds_only_the_audio() -> None:
    """A failing WER keeps the text digest flowing and drops the audio."""
    global AUDIO_VERIFY_PASSED, AUDIO_DELIVERED
    AUDIO_VERIFY_PASSED = False
    AUDIO_DELIVERED = []
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"],
        tts_enabled=True,
        deliver_audio=True,
        audio_verify={"threshold": 0.3},
    )

    result = await _run_workflow(config)

    assert result["status"] == "ok", "the text digest must never be withheld"
    assert result["audio_verification"]["passed"] is False
    assert result["audio_verification"]["verdict"] == "invalid"
    withheld = [
        entry for entry in result["delivery_status"] if entry.get("channel") == "audio_verification"
    ]
    assert withheld and withheld[0]["status"] == "verification_failed"
    assert withheld[0]["score"] == 0.9
    assert AUDIO_DELIVERED == [], "no channel may receive an unverified audio"
    # The text still went out: the digest itself is in the result untouched.
    assert result["digest"] == DIGEST["digest"]
    AUDIO_VERIFY_PASSED = True


async def test_audio_verification_pass_delivers_audio_normally() -> None:
    """A passing WER behaves exactly like an unverified run."""
    global AUDIO_VERIFY_PASSED, AUDIO_DELIVERED
    AUDIO_VERIFY_PASSED = True
    AUDIO_DELIVERED = []
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"],
        tts_enabled=True,
        deliver_audio=True,
        audio_verify={"threshold": 0.3},
    )

    result = await _run_workflow(config)

    assert result["status"] == "ok"
    assert result["audio_verification"]["passed"] is True
    assert result["audio_verification"]["score"] == 0.05
    assert not any(
        entry.get("status") == "verification_failed" for entry in result["delivery_status"]
    )
    assert AUDIO_DELIVERED and AUDIO_DELIVERED[0].endswith(".m4a")


async def test_audio_verification_skipped_without_the_opt_in() -> None:
    """No opt-in (audio_verify=None) means the gate never runs at all."""
    global AUDIO_DELIVERED
    AUDIO_DELIVERED = []
    config = PipelineConfig(
        feed_urls=["https://feed.example/rss"], tts_enabled=True, deliver_audio=True
    )

    result = await _run_workflow(config)

    assert result["audio_verification"] is None
    assert AUDIO_DELIVERED, "audio flows as before when the collection opts out"
