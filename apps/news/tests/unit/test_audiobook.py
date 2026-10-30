"""Audiobook service tests (Fase B2)."""

import subprocess
from pathlib import Path

import pytest

from src.api.services import audiobook as audiobook_module
from src.api.services.audiobook import AudiobookService, split_chapters


def test_split_chapters_markdown_headings() -> None:
    text = "# Capítulo 1\nHola\n\n## Capítulo 2\nMundo\n"
    chapters = split_chapters(text)
    assert len(chapters) >= 2


def test_split_chapters_fallback_blocks() -> None:
    text = "A" * 9000 + "\n\n" + "B" * 100
    chapters = split_chapters(text, max_chars=8000)
    assert len(chapters) >= 2


@pytest.mark.asyncio
async def test_audiobook_uses_lease_gate(tmp_path, monkeypatch) -> None:
    service = AudiobookService(base_dir=str(tmp_path))
    calls = []

    class DummyGate:
        async def should_pause(self):
            return True

        async def yield_and_wait(self):
            calls.append("gate")

    class DummyRes:
        audio_path = str(tmp_path / "ch.mp3")

    class DummyDoc:
        async def synthesize_document(self, **kw):
            Path(DummyRes.audio_path).write_bytes(b"ID3" + b"\x00" * 10)
            return DummyRes()

    monkeypatch.setattr("src.api.services.doc_tts.DocTTSService", lambda *a, **k: DummyDoc())
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] == "ffmpeg":
            if "-f" in cmd and "concat" in cmd:
                Path(cmd[-1]).write_bytes(b"fake")
                return subprocess.CompletedProcess(cmd, 0)
            if "-af" in cmd and "loudnorm" in cmd:
                Path(cmd[-1]).write_bytes(b"fake")
                return subprocess.CompletedProcess(cmd, 0)
            # mux
            Path(cmd[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"1.0\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = await service.render_m4b(
        text="Hola\n\nMundo",
        title="T",
        voice="x",
        language="es",
        gate=DummyGate(),
        resume=False,
    )
    assert calls
    assert res.chapters == 2
    assert res.duration_seconds >= 1.0


@pytest.mark.asyncio
async def test_audiobook_backend_synthesis_goes_through_the_engine(tmp_path, monkeypatch) -> None:
    from src.api.models.narration import TTSBackend

    service = AudiobookService(base_dir=str(tmp_path))
    synth_calls: list[tuple[str, str]] = []
    gates: list[int] = []

    class DummyGate:
        async def should_pause(self):
            return True

        async def yield_and_wait(self):
            gates.append(1)

    async def fake_synth(backend, text, voice, language, *, run_in_modal=None):
        synth_calls.append((str(backend.value), voice))
        return b"RIFF" + b"\x00" * 40

    async def fake_voice_for(backend, voice, language, *, run_in_modal=None):
        return voice or "es-mx-male"

    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)
    monkeypatch.setattr("src.api.services.tts_backends.synthesize", fake_synth)
    monkeypatch.setattr("src.api.services.tts_backends.voice_for", fake_voice_for)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] in {"ffmpeg", "ffprobe"}:
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"1.0\n")
            Path(cmd[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = await service.render_m4b(
        text="# Cap 1\nHola\n\n# Cap 2\nMundo",
        title="T",
        voice="es-mx-male",
        language="es",
        backend=TTSBackend.KOKORO,
        gate=DummyGate(),
        resume=False,
    )
    assert synth_calls, "the selected engine must synthesize the chapters"
    assert all(backend == "kokoro" for backend, _ in synth_calls)
    assert all(voice == "es-mx-male" for _, voice in synth_calls)
    assert gates, "the lease gate must yield before a real synthesis"
    assert res.chapters == 2


class _FakeUpload:
    filename = "libro.epub"

    async def read(self, *args, **kwargs):
        return b"PK-fake-epub-bytes"


@pytest.mark.asyncio
async def test_submit_rejects_a_voice_outside_the_selected_engine(tmp_path, monkeypatch) -> None:
    from src.api.services.doc_tts import SubmitError

    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    with pytest.raises(SubmitError, match="does not belong to engine"):
        await audiobook_module.submit_audiobook(
            _FakeUpload(), "", voice="es-ES-DarioNeural", engine="kokoro"
        )


@pytest.mark.asyncio
async def test_submit_rejects_an_unknown_engine(tmp_path, monkeypatch) -> None:
    from src.api.services.doc_tts import SubmitError

    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    with pytest.raises(SubmitError):
        await audiobook_module.submit_audiobook(_FakeUpload(), "", engine="no-such-engine")


@pytest.mark.asyncio
async def test_submit_forwards_the_engine_to_the_workflow(tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr("src.core.config.settings.audiobook_dir", str(tmp_path / "audiobooks"))
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_audiobook",
        new_callable=AsyncMock,
        return_value="audiobook-1",
    ) as start:
        result = await audiobook_module.submit_audiobook(
            _FakeUpload(),
            "",
            voice="es-mx-male",
            language="es",
            engine="kokoro",
        )
    assert result["workflow_id"].startswith("audiobook-")
    config = start.call_args.args[0]
    assert config.engine == "kokoro"
    assert config.voice == "es-mx-male"


@pytest.mark.parametrize("engine", ["zonos2", "voxcpm2"])
@pytest.mark.asyncio
async def test_submit_rejects_modal_only_engines(tmp_path, monkeypatch, engine) -> None:
    """A book renders for hours: the Modal-only engines are refused up front."""
    from src.api.services.doc_tts import SubmitError

    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr("src.core.config.settings.audiobook_dir", str(tmp_path / "audiobooks"))

    with pytest.raises(SubmitError, match="cannot run on Modal"):
        await audiobook_module.submit_audiobook(_FakeUpload(), "", engine=engine)


@pytest.mark.asyncio
async def test_audiobook_qwen3_renders_through_the_local_gateway(tmp_path, monkeypatch) -> None:
    """qwen3_tts chapters resolve and synthesize with run_in_modal=False."""
    from src.api.models.narration import TTSBackend

    service = AudiobookService(base_dir=str(tmp_path))
    voice_calls: list[tuple[str, object]] = []
    synth_calls: list[tuple[str, object]] = []

    async def fake_voice_for(backend, voice, language, *, run_in_modal=None):
        voice_calls.append((voice, run_in_modal))
        return "uuid-es-anchor"

    async def fake_synth(backend, text, voice, language, *, run_in_modal=None):
        synth_calls.append((voice, run_in_modal))
        assert backend is TTSBackend.QWEN3_TTS
        return b"RIFF" + b"\x00" * 40

    monkeypatch.setattr("src.api.services.tts_backends.voice_for", fake_voice_for)
    monkeypatch.setattr("src.api.services.tts_backends.synthesize", fake_synth)
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] in {"ffmpeg", "ffprobe"}:
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"1.0\n")
            Path(cmd[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = await service.render_m4b(
        text="# Cap 1\nHola",
        title="T",
        voice="es_anchor_a",
        language="es",
        backend=TTSBackend.QWEN3_TTS,
        resume=False,
    )

    assert res.chapters == 1
    assert voice_calls == [("es_anchor_a", False)], "the voice resolves against local qwen3"
    assert synth_calls, "the chapter must synthesize"
    assert all(run_in_modal is False for _, run_in_modal in synth_calls), "never Modal"
    assert all(voice == "uuid-es-anchor" for voice, _ in synth_calls)


def _dummy_doc(monkeypatch, tmp_path: Path) -> None:
    """Make edge synthesis write a tiny audio file without real TTS."""

    class DummyRes:
        audio_path = str(tmp_path / "ch.mp3")

    class DummyDoc:
        async def synthesize_document(self, **kw):
            Path(DummyRes.audio_path).write_bytes(b"ID3" + b"\x00" * 10)
            return DummyRes()

    monkeypatch.setattr("src.api.services.doc_tts.DocTTSService", lambda *a, **k: DummyDoc())


@pytest.mark.asyncio
async def test_failed_mux_never_publishes_a_partial_m4b(tmp_path, monkeypatch) -> None:
    """A failed ffmpeg mux leaves neither the final file nor a .part file."""
    service = AudiobookService(base_dir=str(tmp_path))
    _dummy_doc(monkeypatch, tmp_path)
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] == "ffmpeg":
            if "-f" in cmd and "concat" in cmd:
                Path(cmd[-1]).write_bytes(b"fake")
                return subprocess.CompletedProcess(cmd, 0)
            if "-af" in cmd and "loudnorm" in cmd:
                Path(cmd[-1]).write_bytes(b"fake")
                return subprocess.CompletedProcess(cmd, 0)
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        await service.render_m4b(
            text="Hola\n\nMundo",
            title="T",
            voice="x",
            language="es",
            resume=False,
        )

    audio_dir = tmp_path / "audio"
    assert not list(audio_dir.glob("*.m4b")), "a failed render must not publish an M4B"
    assert not list(audio_dir.glob(".*.part.m4b")), "the temp mux file is cleaned up"


@pytest.mark.asyncio
async def test_cache_key_covers_cover_author_and_loudnorm(tmp_path, monkeypatch) -> None:
    """Changing cover, author or loudnorm produces a different output path."""
    service = AudiobookService(base_dir=str(tmp_path))
    _dummy_doc(monkeypatch, tmp_path)
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    cover_a = tmp_path / "cover-a.jpg"
    cover_b = tmp_path / "cover-b.jpg"
    cover_a.write_bytes(b"jpg")
    cover_b.write_bytes(b"jpg")

    async def render(**kw):
        return await service.render_m4b(
            text="# Cap\nHola", title="T", voice="x", language="es", resume=False, **kw
        )

    first = await render(author="A", cover_path=str(cover_a), loudnorm=True)
    other_author = await render(author="B", cover_path=str(cover_a), loudnorm=True)
    other_cover = await render(author="A", cover_path=str(cover_b), loudnorm=True)
    no_loudnorm = await render(author="A", cover_path=str(cover_a), loudnorm=False)

    assert (
        len({first.m4b_path, other_author.m4b_path, other_cover.m4b_path, no_loudnorm.m4b_path})
        == 4
    )
