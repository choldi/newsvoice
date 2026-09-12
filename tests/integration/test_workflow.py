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
async def stub_filter_new(articles: list[dict]) -> list[dict]:
    return articles


@activity.defn(name="cluster_activity")
async def stub_cluster(articles: list[dict]) -> list[dict]:
    return [CLUSTER]


@activity.defn(name="generate_digest_activity")
async def stub_generate_digest(payload: dict) -> dict:
    return DIGEST


@activity.defn(name="generate_tts_activity")
async def stub_generate_tts(digest: dict) -> dict:
    return AUDIO


@activity.defn(name="deliver_telegram_message_activity")
async def stub_deliver_message(text: str) -> dict:
    return DELIVERY


@activity.defn(name="deliver_telegram_audio_activity")
async def stub_deliver_audio(audio_path: str, caption: str | None) -> dict:
    return DELIVERY


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


async def test_no_new_articles_skips_digest() -> None:
    """When filtering returns nothing, the pipeline stops before the LLM steps."""
    async with await WorkflowEnvironment.start_time_skipping() as env:

        @activity.defn(name="filter_new_activity")
        async def noop_filter(articles: list[dict]) -> list[dict]:
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
