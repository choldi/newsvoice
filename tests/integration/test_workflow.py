"""Integration tests for the DigestPipelineWorkflow using the Temporal test env."""

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig

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


@activity.defn(name="deliver_telegram_message_activity")
async def stub_deliver_message(text: str, chat_id: str | None = None) -> dict:
    return DELIVERY


@activity.defn(name="deliver_telegram_audio_activity")
async def stub_deliver_audio(
    audio_path: str, caption: str | None = None, chat_id: str | None = None
) -> dict:
    return DELIVERY


@activity.defn(name="record_run_activity")
async def stub_record_run(run: dict) -> dict:
    return {"status": "ok", "run_id": 1}


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
                stub_deliver_message,
                stub_deliver_audio,
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
