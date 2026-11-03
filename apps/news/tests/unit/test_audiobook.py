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


@pytest.mark.asyncio
async def test_concat_lists_carry_absolute_paths(tmp_path, monkeypatch) -> None:
    """ffmpeg resolves concat entries against the list file's directory.

    Relative entries duplicated the tmp prefix in the real render (Phase 11.9:
    `tmp/data/audiobooks/..._ch0000.wav` -> exit 254), and every unit test
    mocked ffmpeg, so the lists themselves are asserted here: one long chapter
    forces the inner chunk concat, the second chapter forces the merge list.
    """
    from src.api.models.narration import TTSBackend

    service = AudiobookService(base_dir=str(tmp_path))
    lists: list[str] = []

    async def fake_synth(backend, text, voice, language, *, run_in_modal=None):
        return b"RIFF" + b"\x00" * 40

    async def fake_voice_for(backend, voice, language, *, run_in_modal=None):
        return voice or "es-mx-male"

    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)
    monkeypatch.setattr("src.api.services.tts_backends.synthesize", fake_synth)
    monkeypatch.setattr("src.api.services.tts_backends.voice_for", fake_voice_for)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"1.0\n")
        if cmd[0] == "ffmpeg" and "-f" in cmd and "concat" in cmd:
            list_path = cmd[cmd.index("-i") + 1]
            lists.append(Path(list_path).read_text())
        if cmd[0] in {"ffmpeg", "ffprobe"}:
            Path(cmd[-1]).write_bytes(b"fake")
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    long_chapter = "Hola, esto es una frase larga. " * 90
    res = await service.render_m4b(
        text=f"# Cap 1\n{long_chapter}\n\n# Cap 2\nMundo",
        title="T",
        voice="es-mx-male",
        language="es",
        backend=TTSBackend.KOKORO,
        resume=False,
    )

    assert res.chapters == 2
    assert len(lists) >= 2, "the chunk concat and the chapter merge both write lists"
    for content in lists:
        for line in content.strip().splitlines():
            assert line.startswith("file '/"), f"concat entry must be absolute: {line}"


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


def test_digest_prefix_from_audio_path_handles_ranges() -> None:
    """Old runs only stored ``audio_path``; both name shapes yield the digest."""
    assert (
        audiobook_module.digest_prefix_from_audio_path(
            "/data/audiobooks/audio/adverbios_ba9ddb236f87.m4b"
        )
        == "ba9ddb236f87"
    )
    assert (
        audiobook_module.digest_prefix_from_audio_path(
            "/data/audiobooks/audio/adverbios_ba9ddb236f87_ch0002-0003.m4b"
        )
        == "ba9ddb236f87"
    )
    assert audiobook_module.digest_prefix_from_audio_path(None) == ""
    assert audiobook_module.digest_prefix_from_audio_path("not-an-m4b.mp3") == ""


def test_parse_chapter_range_accepts_empty_and_rejects_bad_values() -> None:
    """The form parser treats blanks as unset and refuses inverted ranges."""
    from src.api.services.doc_tts import SubmitError

    assert audiobook_module.parse_chapter_range(None, None) == (None, None)
    assert audiobook_module.parse_chapter_range("", "  ") == (None, None)
    assert audiobook_module.parse_chapter_range("2", "5") == (2, 5)
    assert audiobook_module.parse_chapter_range(3, None) == (3, None)
    for bad in (("0", None), (None, "0"), ("x", None)):
        with pytest.raises(SubmitError) as exc:
            audiobook_module.parse_chapter_range(*bad)
        assert exc.value.status_code == 422
    with pytest.raises(SubmitError, match="mayor"):
        audiobook_module.parse_chapter_range("5", "2")


@pytest.mark.asyncio
async def test_render_chapter_range_uses_global_indices_and_own_output(
    tmp_path, monkeypatch
) -> None:
    """A slice numbers chapters globally, shares the cache and never overwrites."""
    service = AudiobookService(base_dir=str(tmp_path))
    texts: list[str] = []

    class DummyRes:
        audio_path = str(tmp_path / "ch.mp3")

    class DummyDoc:
        async def synthesize_document(self, **kw):
            texts.append(kw["text"])
            Path(DummyRes.audio_path).write_bytes(b"ID3" + b"\x00" * 10)
            return DummyRes()

    monkeypatch.setattr("src.api.services.doc_tts.DocTTSService", lambda *a, **k: DummyDoc())
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    def fake_run(cmd, check=True, timeout=3600, **kw):
        Path(cmd[-1]).write_bytes(b"fake")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    book = "# Uno\nUno\n\n# Dos\nDos\n\n# Tres\nTres\n\n# Cuatro\nCuatro"
    progress: list[dict] = []
    res = await service.render_m4b(
        text=book,
        title="T",
        voice="x",
        language="es",
        resume=False,
        chapter_range=(2, 3),
        on_progress=progress.append,
    )

    assert (res.chapter_start, res.chapter_end, res.chapters) == (2, 3, 2)
    assert texts == ["Dos", "Tres"], "only the requested chapters synthesize"
    assert Path(res.m4b_path).name.endswith("_ch0002-0003.m4b")
    chapter_names = {p.name.rsplit("_ch", 1)[-1] for p in (tmp_path / "tmp").glob("*_ch????.mp3")}
    assert chapter_names == {"0001.mp3", "0002.mp3"}, "chapter files keep global indices"
    assert progress[0]["chapter_start"] == 2
    assert progress[0]["chapter_end"] == 3
    assert progress[-1]["progress"]["done"] == 2
    assert progress[-1]["progress"]["total"] == 2

    manifest = __import__("json").loads(
        (tmp_path / "tmp" / f"audiobook_{res.digest[:12]}.manifest.json").read_text()
    )
    assert manifest["merged"] is None, "a range never claims the full-book merge"
    assert manifest["ranges"]["2-3"]["merged"].endswith("_ch0002-0003_merged.mp3")
    assert manifest["ranges"]["2-3"]["m4b"].endswith("_ch0002-0003.m4b")

    clamped = await service.render_m4b(
        text=book,
        title="T",
        voice="x",
        language="es",
        resume=True,
        chapter_range=(4, 99),
    )
    assert (clamped.chapter_start, clamped.chapter_end) == (4, 4)
    assert Path(clamped.m4b_path).name.endswith("_ch0004-0004.m4b")
    assert texts[-1] == "Cuatro", "the last chapter is synthesized only once"
    manifest = __import__("json").loads(
        (tmp_path / "tmp" / f"audiobook_{clamped.digest[:12]}.manifest.json").read_text()
    )
    assert set(manifest["ranges"]) == {"2-3", "4-4"}


@pytest.mark.asyncio
async def test_render_chapter_range_outside_the_book_fails(tmp_path, monkeypatch) -> None:
    """A range whose first chapter does not exist is a clear render error."""
    service = AudiobookService(base_dir=str(tmp_path))
    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)

    with pytest.raises(RuntimeError, match="outside this book"):
        await service.render_m4b(
            text="# Uno\nUno\n\n# Dos\nDos",
            title="T",
            voice="x",
            language="es",
            resume=False,
            chapter_range=(9, 10),
        )


def test_build_partial_m4b_uses_the_finished_chapters(tmp_path, monkeypatch) -> None:
    """The preview muxes the chapters already in the manifest and caches it."""
    import json

    service = AudiobookService(base_dir=str(tmp_path))
    prefix = "abcdefabcdef"
    tmp = tmp_path / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    chapter_paths = []
    for idx in range(2):
        path = tmp / f"{prefix}_ch{idx:04d}.mp3"
        path.write_bytes(b"audio")
        chapter_paths.append(path)
    manifest = {
        "digest": prefix,
        "chapters": [{"path": str(path), "done": True, "duration": 1.0} for path in chapter_paths],
        "merged": None,
        "normed": None,
        "ranges": {},
    }
    (tmp / f"audiobook_{prefix}.manifest.json").write_text(json.dumps(manifest))

    monkeypatch.setattr(audiobook_module, "_probe_duration", lambda p: 1.0)
    runs: list[list[str]] = []

    def fake_run(cmd, check=True, timeout=3600, **kw):
        runs.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"fake")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    path, count = service.build_partial_m4b(digest_prefix=prefix, title="Mi libro", loudnorm=False)
    assert count == 2
    assert path.parent.name == "partial"
    assert path.name == "mi-libro_abcdefabcdef_partial0001-0002.m4b"
    assert path.exists()
    assert len(runs) == 2, "one concat and one mux"

    cached, cached_count = service.build_partial_m4b(
        digest_prefix=prefix, title="Mi libro", loudnorm=False
    )
    assert (cached, cached_count) == (path, 2)
    assert len(runs) == 2, "an existing preview is served without running ffmpeg"

    first, first_count = service.build_partial_m4b(
        digest_prefix=prefix, title="Mi libro", loudnorm=False, up_to=1
    )
    assert first_count == 1
    assert first.name.endswith("_partial0001-0001.m4b")


def test_build_partial_m4b_reports_missing_prerequisites(tmp_path, monkeypatch) -> None:
    """Unknown digests/manifests and empty books raise a user-facing error."""
    service = AudiobookService(base_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match="digest"):
        service.build_partial_m4b(digest_prefix="nope", title="T")
    with pytest.raises(RuntimeError, match="manifest"):
        service.build_partial_m4b(digest_prefix="abcdefabcdef", title="T")

    manifest = {"digest": "abcdefabcdef", "chapters": [], "ranges": {}}
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp" / "audiobook_abcdefabcdef.manifest.json").write_text(
        __import__("json").dumps(manifest)
    )
    with pytest.raises(RuntimeError, match="finished chapters"):
        service.build_partial_m4b(digest_prefix="abcdefabcdef", title="T")
