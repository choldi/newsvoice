"""Unit tests for Temporal activities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.api.db.database import AudioJobsRepository
from src.api.models.article import ArticleIn, DigestOut
from src.api.models.cluster import ClusterItem
from src.api.models.tts import TTSOut
from src.workers.activities import (
    cluster_activity,
    compress_audio_activity,
    deliver_logic_activity,
    deliver_telegram_audio_activity,
    deliver_telegram_message_activity,
    fetch_rss_activity,
    filter_new_activity,
    generate_digest_activity,
    generate_logic_activity,
    generate_tts_activity,
    record_run_activity,
    split_message_chunks,
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
            {
                "digest": "# Top Stories",
                "language": "en",
                "voice": "en-gb-female",
                "run_id": "test-tts-run",
                "collection": "health",
            },
        )

    assert result["audio_path"].endswith(".wav")
    assert result["cached"] is False
    tts_in = mock_tts.call_args.args[0]
    assert tts_in.language == "en"
    assert tts_in.voice == "en-gb-female"
    assert tts_in.collection == "health"

    job = await AudioJobsRepository().get_job("test-tts-run")
    assert job is not None
    assert job["status"] == "FINISHED_OK"
    assert job["audio_path"] == "data/tts_cache/abc.wav"
    assert job["elapsed_seconds"] >= 0


async def test_generate_tts_activity_records_error_status() -> None:
    """generate_tts_activity records FINISHED_ERROR when synthesis fails."""
    with patch(
        "src.workers.activities.TTSService.generate_tts",
        new_callable=AsyncMock,
        side_effect=RuntimeError("backend down"),
    ):
        with pytest.raises(ApplicationError, match="TTS generation failed"):
            await ActivityEnvironment().run(
                generate_tts_activity,
                {"digest": "# Top Stories", "run_id": "test-tts-error"},
            )

    job = await AudioJobsRepository().get_job("test-tts-error")
    assert job is not None
    assert job["status"] == "FINISHED_ERROR"
    assert "backend down" in (job["error"] or "")
    assert job["elapsed_seconds"] >= 0


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


async def test_compress_audio_activity_returns_m4a_path() -> None:
    """compress_audio_activity returns the transcoded M4A path."""
    with patch(
        "src.workers.activities.AudioCompressService.compress",
        new_callable=AsyncMock,
        return_value="data/tts_cache/abc.m4a",
    ) as mock_compress:
        result = await ActivityEnvironment().run(compress_audio_activity, "data/tts_cache/abc.wav")

    assert result["audio_path"] == "data/tts_cache/abc.m4a"
    mock_compress.assert_awaited_once_with("data/tts_cache/abc.wav")


async def test_compress_audio_activity_raises_on_failure() -> None:
    """compress_audio_activity surfaces compression errors as non-retryable."""
    with patch(
        "src.workers.activities.AudioCompressService.compress",
        new_callable=AsyncMock,
        side_effect=RuntimeError("ffmpeg not found"),
    ):
        with pytest.raises(ApplicationError, match="Audio compression failed"):
            await ActivityEnvironment().run(compress_audio_activity, "data/tts_cache/abc.wav")


async def test_fetch_rss_activity_skips_failed_feed_without_crashing() -> None:
    """A feed that throws during parse is skipped and does not abort the batch."""
    good_feed = SimpleNamespace(
        feed=SimpleNamespace(title="Tech Feed"),
        entries=[SimpleNamespace(title="Article One", link="https://example.com/1")],
    )
    good_feed.entries[0].get = lambda key, default=None: {
        "summary": "Summary one",
        "published": "2026-09-11T08:00:00",
    }.get(key, default)

    def fake_parse(url: str):
        if "broken" in url:
            raise TimeoutError("timed out")
        return good_feed

    with patch("src.workers.activities.feedparser.parse", side_effect=fake_parse):
        result = await ActivityEnvironment().run(
            fetch_rss_activity, ["https://feed.example/broken", "https://feed.example/good"]
        )

    assert len(result) == 1
    assert result[0]["title"] == "Article One"


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


async def test_deliver_telegram_message_activity_splits_long_text() -> None:
    """Long messages are split into Telegram-safe chunks and sent separately."""
    long_text = "## Story\n\n" + ("Some paragraph text for the digest.\n\n" * 300)
    with patch(
        "src.workers.activities._telegram_request",
        new_callable=AsyncMock,
        return_value={"status": "ok", "message_id": 1},
    ) as mock_request:
        result = await ActivityEnvironment().run(deliver_telegram_message_activity, long_text)

    assert mock_request.await_count > 1
    for call in mock_request.call_args_list:
        assert len(call.args[1]["text"]) <= 4000
    assert result["status"] == "ok"
    assert len(result["message_ids"]) == mock_request.await_count


async def test_deliver_telegram_message_activity_falls_back_to_plain_text() -> None:
    """When Markdown is rejected by Telegram, the chunk is resent as plain text."""
    from temporalio.exceptions import ApplicationError as TemporalError

    with patch(
        "src.workers.activities._telegram_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.side_effect = [
            TemporalError("Telegram API error: can't parse entities", non_retryable=True),
            {"status": "ok", "message_id": 7},
        ]
        result = await ActivityEnvironment().run(
            deliver_telegram_message_activity,
            "## Heading\n\nBody with _unescaped_ markdown",
        )

    assert mock_request.await_count == 2
    first_payload = mock_request.call_args_list[0].args[1]
    second_payload = mock_request.call_args_list[1].args[1]
    assert first_payload["parse_mode"] == "Markdown"
    assert "parse_mode" not in second_payload
    assert result["message_id"] == 7


async def test_split_message_chunks_one_chunk_for_short_text() -> None:
    """Short texts are returned unchanged as a single chunk."""
    chunks = split_message_chunks("## Heading\n\nShort body")
    assert chunks == ["## Heading\n\nShort body"]


async def test_split_message_chunks_respects_max_chars() -> None:
    """Every chunk stays within the character budget."""
    text = "\n\n".join(f"Section {i}: " + ("x" * 80) for i in range(100))
    for chunk in split_message_chunks(text, max_chars=400):
        assert len(chunk) <= 400
    assert len(split_message_chunks(text, max_chars=400)) > 1


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


async def test_deliver_whatsapp_message_activity_calls_service() -> None:
    """deliver_whatsapp_message_activity delegates to WhatsAppService.send_text."""
    from src.workers.activities import deliver_whatsapp_message_activity

    with patch(
        "src.workers.activities.WhatsAppService.send_text",
        new_callable=AsyncMock,
        return_value={"status": "ok", "recipients": 2},
    ) as mock_send:
        result = await ActivityEnvironment().run(
            deliver_whatsapp_message_activity,
            "# digest",
            ["+34600000001"],
            "1234567890@g.us",
        )

    assert result["status"] == "ok"
    assert mock_send.await_count == 1
    assert mock_send.await_args.args[1] == ["+34600000001"]
    assert mock_send.await_args.kwargs["group_id"] == "1234567890@g.us"


async def test_deliver_whatsapp_message_activity_config_error_non_retryable() -> None:
    """An unconfigured gateway surfaces as a non-retryable ApplicationError."""
    from temporalio.exceptions import ApplicationError as TemporalError

    from src.api.services.whatsapp import WhatsAppConfigError
    from src.workers.activities import deliver_whatsapp_message_activity

    with patch(
        "src.workers.activities.WhatsAppService.send_text",
        new_callable=AsyncMock,
        side_effect=WhatsAppConfigError("WhatsApp is not configured"),
    ):
        with pytest.raises(TemporalError) as excinfo:
            await ActivityEnvironment().run(deliver_whatsapp_message_activity, "# digest")
        assert excinfo.value.non_retryable is True


async def test_deliver_whatsapp_audio_activity_calls_service() -> None:
    """deliver_whatsapp_audio_activity delegates to WhatsAppService.send_audio."""
    from src.workers.activities import deliver_whatsapp_audio_activity

    with patch(
        "src.workers.activities.WhatsAppService.send_audio",
        new_callable=AsyncMock,
        return_value={"status": "ok", "recipients": 1},
    ) as mock_send:
        result = await ActivityEnvironment().run(
            deliver_whatsapp_audio_activity,
            "/tmp/digest.m4a",
            "Top",
            ["+34600000001"],
            None,
        )

    assert result["status"] == "ok"
    assert mock_send.await_count == 1
    assert mock_send.await_args.args[0] == "/tmp/digest.m4a"
    assert mock_send.await_args.args[1] == ["+34600000001"]
    assert mock_send.await_args.kwargs["caption"] == "Top"


async def test_generate_logic_activity_generates_logic() -> None:
    """generate_logic_activity delegates to the service and returns the text."""
    cluster = ClusterItem(
        cluster_id=1,
        fingerprint={"entity": "OpenAI", "action": "releases GPT-5", "title_hash": "abc1"},
        articles=[ArticleIn.model_validate(SAMPLE_ARTICLE)],
    )
    with patch(
        "src.workers.activities.DigestService.generate_selection_logic",
        new_callable=AsyncMock,
        return_value="Se eligio OpenAI por impacto y novedad.",
    ) as mock_logic:
        result = await ActivityEnvironment().run(
            generate_logic_activity,
            {
                "clusters": [cluster.model_dump()],
                "digest": "# Daily Digest",
                "prompt_template": "health",
                "language": "es",
            },
        )

    assert result["status"] == "ok"
    assert result["logic"] == "Se eligio OpenAI por impacto y novedad."
    call_kwargs = mock_logic.call_args.kwargs
    assert call_kwargs["prompt_template"] == "health"
    assert call_kwargs["language"] == "es"
    assert len(call_kwargs["clusters"]) == 1


async def test_generate_logic_activity_missing_digest_raises() -> None:
    """generate_logic_activity fails fast when the digest text is missing."""
    with pytest.raises(ApplicationError, match="[Dd]igest text missing"):
        await ActivityEnvironment().run(generate_logic_activity, {"clusters": []})


async def test_generate_logic_activity_surfaces_llm_error() -> None:
    """LLM failures are wrapped in a non-retryable ApplicationError."""
    with patch(
        "src.workers.activities.DigestService.generate_selection_logic",
        new_callable=AsyncMock,
        side_effect=RuntimeError("llm down"),
    ):
        with pytest.raises(ApplicationError, match="logic generation failed"):
            await ActivityEnvironment().run(
                generate_logic_activity, {"clusters": [], "digest": "# D"}
            )


async def test_deliver_logic_activity_telegram_only() -> None:
    """deliver_logic_activity sends the logic text through telegram only."""
    delivery = {"channels": ["telegram"], "chat_id": "chat-override"}
    with patch(
        "src.workers.activities.deliver_telegram_message_activity",
        new_callable=AsyncMock,
        return_value={"status": "ok", "message_id": 99},
    ) as mock_telegram:
        result = await ActivityEnvironment().run(
            deliver_logic_activity, "Logic text", delivery, "health"
        )

    assert result["status"] == "ok"
    assert result["deliveries"] == [{"channel": "telegram", "status": "ok", "message_id": 99}]
    mock_telegram.assert_awaited_once_with("Logic text", "chat-override")


async def test_deliver_logic_activity_email_channel() -> None:
    """deliver_logic_activity emails the logic text with a dedicated subject."""
    delivery = {
        "channels": ["telegram", "email"],
        "email_recipients": ["admin@example.com"],
    }
    with (
        patch(
            "src.workers.activities.deliver_telegram_message_activity",
            new_callable=AsyncMock,
            return_value={"status": "ok", "message_id": 1},
        ),
        patch(
            "src.workers.activities.EmailService.send_digest_email",
            new_callable=AsyncMock,
            return_value={"status": "ok", "recipients": 1},
        ) as mock_email,
    ):
        result = await ActivityEnvironment().run(
            deliver_logic_activity, "Logic text", delivery, "health"
        )

    email_call = mock_email.call_args
    assert email_call.args[:3] == (["admin@example.com"], "Logic text", "health")
    assert "News Selection Logic" in email_call.kwargs["subject"]
    channels = {entry["channel"] for entry in result["deliveries"]}
    assert channels == {"telegram", "email"}


async def test_deliver_logic_activity_records_channel_failure() -> None:
    """A failing channel is recorded without aborting the other channels."""
    from temporalio.exceptions import ApplicationError as TemporalError

    delivery = {
        "channels": ["telegram", "whatsapp"],
        "whatsapp_recipients": ["+34600000001"],
    }
    with (
        patch(
            "src.workers.activities.deliver_telegram_message_activity",
            new_callable=AsyncMock,
            side_effect=TemporalError("Telegram API error", non_retryable=True),
        ),
        patch(
            "src.workers.activities.deliver_whatsapp_message_activity",
            new_callable=AsyncMock,
            return_value={"status": "ok", "recipients": 1},
        ),
    ):
        result = await ActivityEnvironment().run(
            deliver_logic_activity, "Logic text", delivery, "health"
        )

    assert result["status"] == "ok"
    telegram_entry = next(entry for entry in result["deliveries"] if entry["channel"] == "telegram")
    assert telegram_entry["status"] == "error"
    assert "Telegram API error" in telegram_entry["error"]
    assert any(
        entry["channel"] == "whatsapp" and entry["status"] == "ok" for entry in result["deliveries"]
    )
