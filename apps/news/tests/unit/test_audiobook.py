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
        async def before_turn(self):
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
