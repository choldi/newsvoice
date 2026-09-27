"""Unit tests for the narration TTS service (Phase 7.2)."""

import io
import json
import shutil
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.api.models.narration import (
    NarrationScript,
    NarrationSpec,
)
from src.api.services import narration_tts as narration_module
from src.api.services.narration_tts import (
    CANONICAL_FORMAT,
    CANONICAL_RATE,
    MIN_CONVERSATION_TURNS,
    NarrationGatewayError,
    NarrationRenderError,
    NarrationTTSService,
    RenderStrategy,
    build_wav,
    chunk_text,
    concat_wavs,
    content_hash,
    edge_voice_for,
    narration_filename,
    silence_bytes,
    wav_duration,
    wav_format,
)
from src.core.config import settings


def fake_ffmpeg_run(
    payload: bytes,
    returncode: int = 0,
    stderr: bytes = b"",
    calls: list[list[str]] | None = None,
) -> object:
    """Return a ``subprocess.run`` replacement writing payload to the output file.

    Args:
        payload: Bytes ffmpeg should leave in its output target.
        returncode: Exit code to report.
        stderr: Standard error to report.
        calls: Optional list collecting every command line received.

    Returns:
        A callable suitable for ``monkeypatch.setattr(subprocess, "run", ...)``.
    """

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if calls is not None:
            calls.append(command)
        if returncode == 0:
            Path(command[-1]).write_bytes(payload)
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=b"", stderr=stderr
        )

    return _run


def _wav(frames: int = 4800, fmt: object = CANONICAL_FORMAT) -> bytes:
    """Build a WAV payload of silent frames in the given format."""
    silence = b"\x00" * (frames * fmt.frame_size)
    return build_wav(silence, fmt)


def _spec(**overrides: object) -> NarrationSpec:
    """Build a two-role spec using gateway stock voices."""
    payload: dict[str, object] = {
        "title": "Edicion",
        "roles": [
            {"name": "anchor", "voice_id": "es-mx-male"},
            {"name": "guest", "voice_id": "es-mx-female", "speed": 1.1},
        ],
        "pause_ms": 500,
    }
    payload.update(overrides)
    return NarrationSpec.model_validate(payload)


def _script(spec: NarrationSpec, segments: list[dict] | None = None) -> NarrationScript:
    """Build a script validated against the given spec."""
    payload = {
        "segments": segments
        or [
            {"role": "anchor", "text": "Bienvenidos."},
            {"role": "guest", "text": "Gracias por invitarnos."},
        ]
    }
    return NarrationScript.model_validate(payload, context={"spec": spec})


class _StubCompressor:
    """Stands in for AudioCompressService: writes a fake .m4a next to the WAV."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def compress(self, source_path: str) -> str:
        """Create the sibling .m4a and return its path."""
        self.calls.append(source_path)
        target = Path(source_path).with_suffix(".m4a")
        target.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 32)
        return str(target)


def _service(
    tmp_path: Path,
    handler: object,
    *,
    music_dir: Path | None = None,
) -> tuple[NarrationTTSService, _StubCompressor]:
    """Build a service wired to a mock transport and a stub compressor."""
    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    compressor = _StubCompressor()
    service = NarrationTTSService(
        cache_dir=str(tmp_path / "narration"),
        music_dir=str(music_dir or tmp_path / "music"),
        compressor=compressor,
        client=client,
    )
    return service, compressor


# --- WAV primitives -------------------------------------------------------


def test_wav_format_and_duration_round_trip() -> None:
    data = _wav(frames=24000)

    assert wav_format(data) == CANONICAL_FORMAT
    assert wav_duration(data) == 1.0


def test_build_wav_uses_canonical_format_by_default() -> None:
    fmt = wav_format(build_wav(b"\x01\x02" * 100))

    assert (fmt.channels, fmt.sample_width, fmt.framerate) == (1, 2, 24000)


def test_concat_wavs_inserts_pauses_between_parts() -> None:
    parts = [(_wav(frames=24000), 500), (_wav(frames=24000), 0)]

    joined = concat_wavs(parts)

    assert wav_duration(joined) == 2.0 + 0.5
    assert wav_format(joined) == CANONICAL_FORMAT


def test_concat_wavs_ignores_the_trailing_pause() -> None:
    joined = concat_wavs([(_wav(frames=2400), 0), (_wav(frames=2400), 5000)])

    assert wav_duration(joined) == 0.2


def test_concat_wavs_rejects_mismatched_formats() -> None:
    stereo = narration_module.WavFormat(channels=2, sample_width=2, framerate=24000)

    with pytest.raises(NarrationRenderError, match="has format"):
        concat_wavs([(_wav(frames=240), 0), (_wav(240, stereo), 0)])


def test_concat_wavs_requires_parts() -> None:
    with pytest.raises(NarrationRenderError, match="nothing to concatenate"):
        concat_wavs([])


def test_wav_format_rejects_garbage() -> None:
    with pytest.raises(NarrationRenderError, match="invalid WAV payload"):
        wav_format(b"definitely not a wav")


def test_silence_bytes_length_and_zero_duration() -> None:
    assert len(silence_bytes(0)) == 0
    assert len(silence_bytes(1000)) == CANONICAL_RATE * CANONICAL_FORMAT.frame_size


# --- Chunking -------------------------------------------------------------


def test_chunk_text_keeps_short_text_intact() -> None:
    assert chunk_text("Hola mundo", 1500) == ["Hola mundo"]


def test_chunk_text_packs_sentences() -> None:
    text = "Primera frase. " * 40

    chunks = chunk_text(text, 100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_chunk_text_breaks_long_sentences_on_clauses() -> None:
    text = ", ".join(["palabra"] * 60) + "."

    chunks = chunk_text(text, 50)

    assert all(len(chunk) <= 50 for chunk in chunks)
    assert all(not chunk.startswith(" ") for chunk in chunks)


def test_chunk_text_breaks_on_words_when_needed() -> None:
    text = "x" * 500

    chunks = chunk_text(text, 60)

    assert all(len(chunk) <= 60 for chunk in chunks)
    assert "".join(chunks) == text


def test_chunk_text_rejects_empty_text_and_bad_size() -> None:
    with pytest.raises(NarrationRenderError, match="empty text"):
        chunk_text("   ", 100)
    with pytest.raises(NarrationRenderError, match="max_chars must be positive"):
        chunk_text("hola", 0)


# --- Naming, hashing and voice resolution ---------------------------------


def test_narration_filename_includes_collection_date_and_hash() -> None:
    day = datetime(2026, 9, 26, tzinfo=UTC)

    name = narration_filename("Tech News!", day, "abcdef1234567890")

    assert name == "narrate_tech_news_260926_abcdef12.wav"


def test_narration_filename_defaults_to_default_collection() -> None:
    assert narration_filename("  ", None, "ff").startswith("narrate_default_")


def test_content_hash_changes_with_script_voice_and_strategy() -> None:
    spec = _spec()
    script = _script(spec)
    other = _script(
        spec, [{"role": "anchor", "text": "Bienvenidos."}, {"role": "guest", "text": "Otro."}]
    )
    faster = _spec(speed=1.3)

    base = content_hash(spec, script)

    assert len(base) == 64
    assert content_hash(spec, other) != base
    assert content_hash(faster, script) != base
    assert content_hash(spec, script, RenderStrategy.CONVERSATIONS) != content_hash(
        spec, script, RenderStrategy.TURNS
    )


def test_edge_voice_for_maps_aliases_names_and_defaults() -> None:
    assert edge_voice_for("es-mx-male", "es") == "es-MX-JorgeNeural"
    assert edge_voice_for("es-ES-ElviraNeural", "es") == "es-ES-ElviraNeural"
    assert edge_voice_for("zz-unknown", "en") == "en-US-GuyNeural"
    assert edge_voice_for("zz-unknown", "xx") == "es-MX-JorgeNeural"


# --- Gateway client -------------------------------------------------------


async def test_health_reports_engine_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/health")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "engine": "routed(edgetts, pocket_tts)",
                "device": "routed",
                "mode": "ROUTED",
                "stock_voices_count": 28,
                "gpu_available": False,
            },
        )

    service, _ = _service(tmp_path, handler)

    health = await service.health()

    assert health.reachable is True
    assert health.status == "ok"
    assert health.stock_voices_count == 28
    assert health.error is None
    await service.aclose()


async def test_health_never_raises_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    service, _ = _service(tmp_path, handler)

    health = await service.health()

    assert health.reachable is False
    assert health.status == "unreachable"
    assert health.error is not None
    await service.aclose()


async def test_health_flags_a_degraded_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "degraded", "engine": "mock"})

    service, _ = _service(tmp_path, handler)

    health = await service.health()

    assert health.status == "degraded"
    assert health.error == "gateway status is degraded"
    await service.aclose()


async def test_list_voices_filters_by_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"voice_id": "es-mx-male", "language": "es", "gender": "male"},
                {"voice_id": "en-us-male", "language": "en", "gender": "male"},
            ],
        )

    service, _ = _service(tmp_path, handler)

    assert [v["voice_id"] for v in await service.list_voices("es")] == ["es-mx-male"]
    assert len(await service.list_voices()) == 2
    await service.aclose()


async def test_list_voices_returns_empty_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("boom", request=request)

    service, _ = _service(tmp_path, handler)

    assert await service.list_voices("es") == []
    await service.aclose()


async def test_gateway_error_body_is_parsed_into_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "detail": {
                    "detail": "Voz no encontrada: zz-9999 (stock)",
                    "error_type": "voice_not_found",
                    "valid_voice_ids": ["es-mx-male", "es-mx-female"],
                }
            },
        )

    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    service, _ = _service(tmp_path, handler)

    with pytest.raises(NarrationGatewayError) as excinfo:
        await service.synthesize_turn("hola", "zz-9999", "es")

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_type == "voice_not_found"
    assert "valid voice ids: es-mx-male, es-mx-female" in str(excinfo.value)
    await service.aclose()


async def test_empty_gateway_body_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    service, _ = _service(tmp_path, handler)

    with pytest.raises(NarrationGatewayError, match="empty body"):
        await service.synthesize_turn("hola", "es-mx-male", "es")
    await service.aclose()


async def test_synthesize_turn_sends_the_documented_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_wav())

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    audio, used_edge = await service.synthesize_turn(
        "Hola", "es-mx-male", "es", speed=1.2, emotion="calm"
    )

    assert used_edge is False
    assert wav_format(audio) == CANONICAL_FORMAT
    assert seen == {
        "text": "Hola",
        "voice_id": "es-mx-male",
        "language": "es",
        "speed": 1.2,
        "emotion": "calm",
    }
    await service.aclose()


async def test_omitted_emotion_is_not_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_wav())

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    await service.synthesize_turn("Hola", "es-mx-male", "es")

    assert "emotion" not in seen
    await service.aclose()


async def test_api_key_is_sent_as_a_header_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-API-Key")
        return httpx.Response(200, content=_wav())

    service, _ = _service(tmp_path, handler)
    service._api_key = "secret-key"
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    await service.synthesize_turn("Hola", "es-mx-male", "es")

    assert service._headers()["X-API-Key"] == "secret-key"
    await service.aclose()


async def test_gateway_failure_falls_back_to_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"detail": "engine down"}})

    service, _ = _service(tmp_path, handler)
    service._api_key = ""
    monkeypatch.setattr(settings, "narration_fallback_to_edge", True)

    class _FakeCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            self.text = text
            self.voice = voice

        async def stream(self) -> object:
            yield {"type": "audio", "data": b"edge-mp3"}

    monkeypatch.setattr(narration_module.edge_tts, "Communicate", _FakeCommunicate)
    ffmpeg_calls: list[list[str]] = []
    monkeypatch.setattr(
        narration_module.subprocess,
        "run",
        fake_ffmpeg_run(build_wav(b"\x01\x00" * 2400), calls=ffmpeg_calls),
    )

    audio, used_edge = await service.synthesize_turn("Hola", "zz-9000", "en")

    assert used_edge is True
    assert wav_format(audio) == CANONICAL_FORMAT
    assert ffmpeg_calls and "pipe:0" in ffmpeg_calls[0]
    await service.aclose()


async def test_gateway_failure_without_fallback_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"detail": "engine down"}})

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    async def _boom(text: str, voice: str) -> bytes:
        raise AssertionError("edge fallback must not run")

    monkeypatch.setattr(service, "_edge_synthesize", _boom)

    with pytest.raises(NarrationGatewayError, match="engine down"):
        await service.synthesize_turn("Hola", "es-mx-male", "es")
    await service.aclose()


async def test_edge_fallback_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"detail": "engine down"}})

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", True)

    async def _boom(text: str, voice: str) -> bytes:
        raise RuntimeError("no network")

    monkeypatch.setattr(service, "_edge_synthesize", _boom)

    with pytest.raises(NarrationGatewayError, match="Edge fallback failed"):
        await service.synthesize_turn("Hola", "es-mx-male", "es")
    await service.aclose()


async def test_synthesize_conversation_sends_turns_and_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_wav())

    service, _ = _service(tmp_path, handler)

    audio = await service.synthesize_conversation(
        [{"voice_id": "es-mx-male", "text": "uno", "language": "es"}], 300
    )

    assert wav_format(audio) == CANONICAL_FORMAT
    assert seen["pause_ms"] == 300
    assert seen["turns"][0]["voice_id"] == "es-mx-male"
    await service.aclose()


# --- Render ---------------------------------------------------------------


async def test_render_assembles_turns_with_pauses_and_writes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=_wav(frames=2400))

    service, compressor = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()
    script = _script(spec)

    result = await service.render(spec, script, collection="tech")

    assert [c["voice_id"] for c in calls] == ["es-mx-male", "es-mx-female"]
    assert calls[0]["speed"] == 1.0
    assert calls[1]["speed"] == 1.1
    assert result.cached is False
    assert result.used_conversations_api is False
    assert result.segments == 2
    assert result.voices == ["es-mx-male", "es-mx-female"]
    # 0.1 s per turn plus a 0.5 s pause
    assert result.duration_seconds == pytest.approx(0.7, abs=0.01)
    assert Path(result.wav_path).exists()
    assert Path(result.audio_path).exists()
    assert compressor.calls == [result.wav_path]

    meta = json.loads(Path(result.wav_path).with_suffix(".meta.json").read_text())
    assert meta["key"] == content_hash(spec, script, RenderStrategy.TURNS)
    assert meta["segments"] == 2
    assert meta["collection"] == "tech"
    await service.aclose()


async def test_render_strips_emotion_tags_from_the_spoken_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()
    script = _script(
        spec,
        [
            {"role": "anchor", "text": "[happy] Bienvenidos.", "emotion": "happy"},
            {"role": "guest", "text": "Gracias."},
        ],
    )

    await service.render(spec, script)

    assert calls[0]["text"] == "Bienvenidos."
    assert calls[0]["emotion"] == "happy"
    await service.aclose()


async def test_render_uses_segment_pause_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec(pause_ms=500)
    script = _script(
        spec,
        [
            {"role": "anchor", "text": "Uno.", "pause_after_ms": 1000},
            {"role": "guest", "text": "Dos."},
        ],
    )

    result = await service.render(spec, script)

    assert result.duration_seconds == pytest.approx(1.2, abs=0.01)
    await service.aclose()


async def test_render_chunks_long_segments_into_several_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "narration_max_chars_per_request", 40)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()
    script = _script(
        spec,
        [
            {"role": "anchor", "text": "Primera frase del guion. " * 4},
            {"role": "guest", "text": "Corto."},
        ],
    )

    await service.render(spec, script)

    anchor_calls = [c for c in calls if c["voice_id"] == "es-mx-male"]
    assert len(anchor_calls) > 1
    assert all(len(c["text"]) <= 40 for c in calls)
    await service.aclose()


async def test_render_reuses_the_cache_for_identical_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()
    script = _script(spec)

    first = await service.render(spec, script, collection="tech")
    second = await service.render(spec, script, collection="tech")

    assert first.cached is False
    assert second.cached is True
    assert len(calls) == 2
    assert second.audio_path == first.audio_path
    assert second.duration_seconds == first.duration_seconds
    await service.aclose()


async def test_render_regenerates_when_the_script_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()

    first = await service.render(
        spec,
        _script(spec, [{"role": "anchor", "text": "Uno."}, {"role": "guest", "text": "Dos."}]),
    )
    second = await service.render(
        spec,
        _script(spec, [{"role": "anchor", "text": "Otro."}, {"role": "guest", "text": "Dos."}]),
    )

    assert first.wav_path != second.wav_path
    assert second.cached is False
    await service.aclose()


async def test_render_ignores_a_stale_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()
    script = _script(spec)
    result = await service.render(spec, script)

    sidecar = Path(result.wav_path).with_suffix(".meta.json")
    meta = json.loads(sidecar.read_text())
    meta["key"] = "0" * 64
    sidecar.write_text(json.dumps(meta))
    Path(result.audio_path).unlink()

    again = await service.render(spec, script)

    assert again.cached is False
    await service.aclose()


async def test_render_rejects_segments_naming_an_unknown_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    spec = _spec()
    script = NarrationScript(segments=[{"role": "ghost", "text": "hola"}])

    with pytest.raises(Exception, match="unknown roles"):
        await service.render(spec, script)
    await service.aclose()


async def test_render_resamples_a_non_canonical_gateway_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = narration_module.WavFormat(channels=1, sample_width=2, framerate=22050)
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2205, fmt=other))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    monkeypatch.setattr(
        narration_module.subprocess,
        "run",
        fake_ffmpeg_run(_wav(frames=2400), calls=calls),
    )
    spec = _spec()

    result = await service.render(spec, _script(spec))

    assert calls, "a 22050 Hz turn must be resampled"
    assert "pipe:0" in calls[0]
    assert wav_format(Path(result.wav_path).read_bytes()) == CANONICAL_FORMAT
    await service.aclose()


async def test_render_falls_back_per_turn_and_counts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["voice_id"] == "es-mx-female":
            return httpx.Response(404, json={"detail": {"detail": "no such voice"}})
        return httpx.Response(200, content=_wav(frames=2400))

    monkeypatch.setattr(settings, "narration_fallback_to_edge", True)
    monkeypatch.setattr(settings, "narrations_dialog_fallback_to_edge", True)
    service, _ = _service(tmp_path, handler)

    async def fake_edge(text: str, voice: str) -> bytes:
        assert voice == "es-MX-DaliaNeural", "legacy names map through the alias table"
        return _wav(frames=2400)

    service._edge_synthesize = fake_edge  # type: ignore[method-assign]
    spec = _spec()

    result = await service.render(spec, _script(spec))

    assert result.edge_fallbacks == 1
    await service.aclose()


async def test_render_mixes_music_under_the_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    (music_dir / "loop.mp3").write_bytes(b"fake-mp3")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=24000))

    service, _ = _service(tmp_path, handler, music_dir=music_dir)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    monkeypatch.setattr(
        narration_module.subprocess,
        "run",
        fake_ffmpeg_run(_wav(frames=24000), calls=calls),
    )
    spec = _spec(music={"track": "loop.mp3", "volume_db": -20.0, "duck_db": -9.0})

    result = await service.render(spec, _script(spec))

    assert result.music == "loop.mp3"
    mix_call = next(call for call in calls if "-filter_complex" in call)
    graph = mix_call[mix_call.index("-filter_complex") + 1]
    assert "asplit=2" in graph, "the voice is needed by both the mix and the sidechain"
    assert "sidechaincompress" in graph
    assert "volume=-20.0dB" in graph
    assert str(music_dir / "loop.mp3") in mix_call
    await service.aclose()


async def test_render_keeps_the_voice_when_the_music_mix_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    (music_dir / "loop.mp3").write_bytes(b"fake-mp3")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=24000))

    service, _ = _service(tmp_path, handler, music_dir=music_dir)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)

    monkeypatch.setattr(
        narration_module.subprocess,
        "run",
        fake_ffmpeg_run(b"", returncode=1, stderr=b"Invalid stream specifier"),
    )
    spec = _spec(music={"track": "loop.mp3"})

    result = await service.render(spec, _script(spec))

    assert Path(result.wav_path).exists()
    assert result.duration_seconds == pytest.approx(2.5, abs=0.01)
    await service.aclose()


async def test_render_requires_an_existing_music_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec(music={"track": "missing.mp3"})

    with pytest.raises(NarrationRenderError, match="music track not found"):
        await service.render(spec, _script(spec))
    await service.aclose()


async def test_conversations_strategy_rejects_per_role_speeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()  # the guest role overrides speed

    with pytest.raises(NarrationRenderError, match="single speaking rate"):
        await service.render(
            spec, _script(spec), collection="tech", strategy=RenderStrategy.CONVERSATIONS
        )
    await service.aclose()


async def test_conversations_strategy_rejects_emotions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec(
        roles=[
            {"name": "anchor", "voice_id": "es-mx-male"},
            {"name": "guest", "voice_id": "es-mx-female"},
        ]
    )

    with pytest.raises(NarrationRenderError, match="ignores emotions"):
        await service.render(
            spec,
            _script(
                spec,
                segments=[
                    {"role": "anchor", "text": "Bienvenidos."},
                    {"role": "guest", "text": "Gracias.", "emotion": "happy"},
                ],
            ),
            collection="tech",
            strategy=RenderStrategy.CONVERSATIONS,
        )
    await service.aclose()


async def test_render_with_the_conversations_strategy_uses_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec(
        roles=[{"name": "anchor", "voice_id": "es-mx-male"}]
        + _spec().roles[1:1]
        + [{"name": "guest", "voice_id": "es-mx-female"}]
    )

    result = await service.render(
        spec, _script(spec), collection="tech", strategy=RenderStrategy.CONVERSATIONS
    )

    assert len(calls) == 1
    assert calls[0][0].endswith("/conversations")
    assert [turn["voice_id"] for turn in calls[0][1]["turns"]] == ["es-mx-male", "es-mx-female"]
    assert result.used_conversations_api is True
    assert result.duration_seconds == pytest.approx(0.1, abs=0.01)
    await service.aclose()


async def test_conversations_strategy_refuses_emotions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    spec = _spec()
    script = _script(
        spec,
        [
            {"role": "anchor", "text": "Uno.", "emotion": "calm"},
            {"role": "guest", "text": "Dos."},
        ],
    )

    with pytest.raises(NarrationRenderError, match="ignores emotions"):
        await service.render(spec, script, strategy=RenderStrategy.CONVERSATIONS)
    await service.aclose()


async def test_conversations_strategy_needs_two_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    spec = _spec()
    script = _script(spec, [{"role": "anchor", "text": "Solo un turno."}])

    with pytest.raises(NarrationRenderError, match="at least 2 turns"):
        await service.render(spec, script, strategy=RenderStrategy.CONVERSATIONS)
    assert MIN_CONVERSATION_TURNS == 2
    await service.aclose()


async def test_render_uses_the_day_in_the_cache_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler)
    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    spec = _spec()

    result = await service.render(
        spec, _script(spec), collection="tech", day=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert "260102" in Path(result.wav_path).name
    await service.aclose()


def test_build_wav_helper_is_usable_with_stdlib(tmp_path: Path) -> None:
    path = tmp_path / "out.wav"
    path.write_bytes(build_wav(b"\x00" * 480, CANONICAL_FORMAT))

    with wave.open(io.BytesIO(path.read_bytes()), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnframes() == 240


# --- Real ffmpeg ----------------------------------------------------------
# The fake-ffmpeg tests above only prove the command line is built; these prove
# the produced audio is actually correct (the music mix once produced a 24-hour
# file because ffmpeg cannot infer the duration of a WAV arriving on a pipe).

needs_ffmpeg = pytest.mark.skipif(
    shutil.which(settings.ffmpeg_path) is None, reason="ffmpeg is not installed"
)


def _real_track(path: Path) -> Path:
    """Create a short music file with ffmpeg's synthetic sine source."""
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=30",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@needs_ffmpeg
async def test_real_music_mix_keeps_the_narration_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    _real_track(music_dir / "loop.wav")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=24000))

    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    service = NarrationTTSService(
        cache_dir=str(tmp_path / "narration"),
        music_dir=str(music_dir),
        compressor=_StubCompressor(),
        client=client,
    )
    spec = _spec(pause_ms=200, music={"track": "loop.wav", "volume_db": -20.0})

    result = await service.render(spec, _script(spec))

    assert result.duration_seconds == pytest.approx(2.2, abs=0.05)
    mixed = Path(result.wav_path).read_bytes()
    assert wav_format(mixed) == CANONICAL_FORMAT
    # The music is really there: a voice-only render is all zeroes.
    window = mixed[len(mixed) // 2 : len(mixed) // 2 + 2000]
    assert window != b"\x00" * 2000, "the music bed must be audible under the voice"
    await service.aclose()


@needs_ffmpeg
async def test_real_m4a_is_produced_and_playable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    service = NarrationTTSService(
        cache_dir=str(tmp_path / "narration"),
        music_dir=str(tmp_path / "music"),
        client=client,
    )
    spec = _spec()

    result = await service.render(spec, _script(spec))

    payload = Path(result.audio_path).read_bytes()
    assert payload[4:8] == b"ftyp"
    probe = subprocess.run(
        [settings.ffmpeg_path, "-hide_banner", "-i", result.audio_path],
        capture_output=True,
        text=True,
    )
    assert "Audio: aac" in probe.stderr
    assert "24000 Hz" in probe.stderr
    await service.aclose()


@needs_ffmpeg
async def test_real_resample_of_a_foreign_sample_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = narration_module.WavFormat(channels=1, sample_width=2, framerate=22050)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=22050, fmt=other))

    monkeypatch.setattr(settings, "narration_fallback_to_edge", False)
    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    service = NarrationTTSService(
        cache_dir=str(tmp_path / "narration"),
        music_dir=str(tmp_path / "music"),
        compressor=_StubCompressor(),
        client=client,
    )
    spec = _spec(pause_ms=0)

    result = await service.render(spec, _script(spec))

    # 1 s per turn at 22050 Hz resampled to 24 kHz, no pause between them.
    assert result.duration_seconds == pytest.approx(2.0, abs=0.05)
    assert wav_format(Path(result.wav_path).read_bytes()) == CANONICAL_FORMAT
    await service.aclose()


async def test_music_mix_graph_applies_the_configured_duck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    (music_dir / "bed.wav").write_bytes(_wav(frames=24000))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    calls: list[list[str]] = []
    monkeypatch.setattr(
        narration_module.subprocess, "run", fake_ffmpeg_run(_wav(frames=2400), calls=calls)
    )
    service, _ = _service(tmp_path, handler, music_dir=music_dir)
    spec = _spec(
        music={"track": "bed.wav", "volume_db": -18.0, "duck_db": -12.0, "fade_out_ms": 500}
    )

    await service.render(spec, _script(spec))

    graph = calls[0][calls[0].index("-filter_complex") + 1]
    assert "volume=-18.0dB" in graph, "the bed volume is applied"
    assert "sidechaincompress" in graph, "the bed is ducked by the voice"
    assert "[ducked]volume=-12.0dB[ducked2]" in graph, "duck_db is applied as attenuation"
    assert "amix=inputs=2:duration=first" in graph
    await service.aclose()


async def test_music_track_cannot_escape_the_music_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    (tmp_path / "secret.wav").write_bytes(_wav(frames=2400))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wav(frames=2400))

    service, _ = _service(tmp_path, handler, music_dir=music_dir)
    spec = _spec(music={"track": "../secret.wav"})

    with pytest.raises(NarrationRenderError, match="music track not found"):
        await service.render(spec, _script(spec))
    await service.aclose()
