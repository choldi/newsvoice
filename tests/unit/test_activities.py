"""Unit tests for Temporal activities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from temporalio.testing import ActivityEnvironment

from src.api.models.article import ArticleIn, DigestOut
from src.api.models.cluster import ClusterItem
from src.api.models.tts import TTSOut
from src.workers.activities import (
    cluster_activity,
    deliver_telegram_audio_activity,
    deliver_telegram_message_activity,
    fetch_rss_activity,
    filter_new_activity,
    generate_digest_activity,
    generate_tts_activity,
    record_run_activity,
)

SAMPLE_ARTICLE = {
    "title": "OpenAI releases GPT-5",
    "link": "https://example.com/gpt5",
    "content": "OpenAI announced GPT-5 today.",
    "source": "TechCrunch",
}


async def test_filter_new_activity_returns_new_articles() -> None:
    """filter_new_activity returns dumped article dicts from the service."""
    service_return = [ArticleIn.model_validate(SAMPLE_ARTICLE)]
    with patch(
        "src.workers.activities.DedupService.filter_new_articles",
        new_callable=AsyncMock,
        return_value=service_return,
    ):
        result = await ActivityEnvironment().run(filter_new_activity, [SAMPLE_ARTICLE])

    assert result == [SAMPLE_ARTICLE | {"pub_date": None}]


async def test_cluster_activity_returns_clusters() -> None:
    """cluster_activity returns dumped cluster dicts from the service."""
    service_return = [
        ClusterItem(
            cluster_id=1,
            fingerprint={
                "entity": "OpenAI",
                "action": "releases GPT-5",
                "title_hash": "abc1",
            },
            articles=[ArticleIn.model_validate(SAMPLE_ARTICLE)],
            is_material_update=True,
        )
    ]
    with patch(
        "src.workers.activities.ClusterService.cluster_articles",
        new_callable=AsyncMock,
        return_value=service_return,
    ):
        result = await ActivityEnvironment().run(cluster_activity, [SAMPLE_ARTICLE])

    assert len(result) == 1
    assert result[0]["fingerprint"]["entity"] == "OpenAI"
    assert result[0]["is_material_update"] is True


async def test_generate_digest_activity_returns_digest() -> None:
    """generate_digest_activity validates input and returns generated digest."""
    with patch(
        "src.workers.activities.DigestService.generate_digest",
        new_callable=AsyncMock,
        return_value=DigestOut(status="ok", digest="# Top Stories", article_count=1),
    ) as mock_digest:
        result = await ActivityEnvironment().run(
            generate_digest_activity,
            {"articles": [SAMPLE_ARTICLE], "language": "es"},
        )

    assert result["digest"] == "# Top Stories"
    assert result["article_count"] == 1
    digest_in = mock_digest.call_args.args[0]
    assert digest_in.language == "es"


async def test_generate_tts_activity_returns_audio_path() -> None:
    """generate_tts_activity converts the digest markdown into audio metadata."""
    with patch(
        "src.workers.activities.TTSService.generate_tts",
        new_callable=AsyncMock,
        return_value=TTSOut(
            status="ok",
            audio_path="data/tts_cache/abc.wav",
            duration_estimate=10.0,
            cached=False,
            voice="es-mx-male",
            char_count=150,
        ),
    ) as mock_tts:
        result = await ActivityEnvironment().run(
            generate_tts_activity,
            {"digest": "# Top Stories", "language": "en", "voice": "en-gb-female"},
        )

    assert result["audio_path"].endswith(".wav")
    assert result["cached"] is False
    tts_in = mock_tts.call_args.args[0]
    assert tts_in.language == "en"
    assert tts_in.voice == "en-gb-female"


async def test_generate_tts_activity_falls_back_to_settings() -> None:
    """generate_tts_activity uses settings defaults when payload omits them."""
    with (
        patch(
            "src.workers.activities.TTSService.generate_tts",
            new_callable=AsyncMock,
            return_value=TTSOut(
                status="ok",
                audio_path="data/tts_cache/abc.wav",
                duration_estimate=5.0,
                cached=False,
                voice="es-mx-male",
                char_count=100,
            ),
        ) as mock_tts,
        patch("src.workers.activities.get_settings") as mock_settings,
    ):
        mock_settings.return_value.tts_language = "es"
        mock_settings.return_value.tts_voice = "es-mx-male"
        await ActivityEnvironment().run(generate_tts_activity, {"digest": "# Top Stories"})

    tts_in = mock_tts.call_args.args[0]
    assert tts_in.language == "es"
    assert tts_in.voice == "es-mx-male"


async def test_fetch_rss_activity_parses_entries() -> None:
    """fetch_rss_activity normalizes feed entries into article dicts."""

    class FakeEntry:
        def __init__(self, title: str, link: str, summary: str, published: str) -> None:
            self.title = title
            self.link = link
            self._summary = summary
            self._published = published

        def get(self, key: str, default: str | None = None) -> str | None:
            return {"summary": self._summary, "published": self._published}.get(key, default)

    fake_feed = SimpleNamespace(
        feed=SimpleNamespace(title="Tech Feed"),
        entries=[
            FakeEntry(
                title="Article One",
                link="https://example.com/1",
                summary="Summary one",
                published="2026-09-11T08:00:00",
            ),
            FakeEntry(title="", link="https://example.com/skip", summary="", published=""),
        ],
    )
    with patch("src.workers.activities.feedparser.parse", return_value=fake_feed):
        result = await ActivityEnvironment().run(fetch_rss_activity, ["https://feed.example/rss"])

    assert len(result) == 1
    assert result[0]["title"] == "Article One"
    assert result[0]["link"] == "https://example.com/1"
    assert result[0]["source"] == "Tech Feed"


async def test_deliver_telegram_message_activity_sends_text() -> None:
    """deliver_telegram_message_activity strips emotion tags before sending."""
    with patch(
        "src.workers.activities._telegram_request",
        new_callable=AsyncMock,
        return_value={"status": "ok", "message_id": 42},
    ) as mock_request:
        result = await ActivityEnvironment().run(
            deliver_telegram_message_activity, "[happy] Top Stories [calm] continue"
        )

    sent_payload = mock_request.call_args.args[1]
    assert "[happy]" not in sent_payload
    assert result["message_id"] == 42


async def test_deliver_telegram_audio_activity_posts_file() -> None:
    """deliver_telegram_audio_activity uploads the WAV to Telegram as audio/wav."""

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"ok": True, "result": {"message_id": 7}}

    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            captured["files"] = kwargs.get("files")
            return FakeResponse()

    audio_file = "/tmp/opencode/fake_audio.wav"
    with open(audio_file, "wb") as fh:
        fh.write(b"fake-wav")

    with (
        patch("src.workers.activities.httpx.AsyncClient", FakeAsyncClient),
        patch("src.workers.activities.get_settings") as mock_settings,
    ):
        mock_settings.return_value.telegram_bot_token = "token"
        mock_settings.return_value.telegram_chat_id = "chat"
        result = await ActivityEnvironment().run(
            deliver_telegram_audio_activity, audio_file, caption="[happy] Gracias"
        )

    files = captured["files"]
    assert isinstance(files, dict)
    assert files["audio"][2] == "audio/wav"
    assert result["message_id"] == 7


async def test_record_run_activity_persists_run() -> None:
    """record_run_activity calls RunsRepository.record_run with the run dict."""
    with patch(
        "src.workers.activities.RunsRepository.record_run",
        new_callable=AsyncMock,
        return_value=17,
    ) as mock_record:
        run = {
            "workflow_id": "workflow-1",
            "collection": "tech",
            "status": "ok",
            "article_count": 3,
            "cluster_count": 2,
            "digest_preview": "# Summary",
            "audio_path": None,
            "error": None,
        }
        result = await ActivityEnvironment().run(record_run_activity, run)

    mock_record.assert_awaited_once_with(**run)
    assert result["status"] == "ok"
    assert result["run_id"] == 17
