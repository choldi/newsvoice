"""Unit tests for the voice catalog activities (Phase 7.8)."""

import json
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.core import voice_catalog
from src.core.audio import CANONICAL_FORMAT, build_wav
from src.core.config import settings
from src.workers import voice_activities

WAV = build_wav(b"\xff\x7f" * 2400, CANONICAL_FORMAT)


def _profile(name: str = "anchor_neutral_m") -> dict[str, Any]:
    return {
        "name": name,
        "label": f"Voice {name}",
        "instruct": "Male, late 40s, warm baritone, broadcast clarity",
        "ref_text": "Welcome to the daily briefing. " * 6,
        "language": "en",
    }


async def test_design_voice_activity_stores_reference_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_design(**kwargs: object) -> tuple[bytes, int]:
        assert kwargs["instruct"] == _profile()["instruct"]
        return WAV, 24000

    monkeypatch.setattr("src.core.modal_client.modal_design_voice", fake_design)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "qwen_tts_design_model", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")

    result = await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())

    assert result["name"] == "anchor_neutral_m"
    assert result["sample_rate"] == 24000
    assert result["sha256"] == voice_catalog.sha256_bytes(WAV)
    ref_path = tmp_path / "voices" / "refs" / "anchor_neutral_m.wav"
    assert ref_path.read_bytes() == WAV
    manifest = json.loads((tmp_path / "voices" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["anchor_neutral_m"]["instruct_sha256"] == voice_catalog.sha256_text(
        _profile()["instruct"]
    )


async def test_design_voice_activity_rejects_incomplete_payload() -> None:
    with pytest.raises(ApplicationError, match="needs name, instruct and ref_text") as excinfo:
        await ActivityEnvironment().run(
            voice_activities.design_voice_activity, {"name": "anchor_neutral_m"}
        )
    assert excinfo.value.non_retryable is True


async def test_design_voice_activity_wraps_modal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_design(**kwargs: object) -> tuple[bytes, int]:
        raise RuntimeError("modal down")

    monkeypatch.setattr("src.core.modal_client.modal_design_voice", failing_design)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))

    with pytest.raises(ApplicationError, match="voice design failed"):
        await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())


async def test_upload_voice_activity_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_dir = tmp_path / "voices" / "refs"
    ref_dir.mkdir(parents=True)
    (ref_dir / "anchor_neutral_m.wav").write_bytes(WAV)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    seen: dict[str, Any] = {}

    async def fake_upload(name: str, wav: bytes) -> None:
        seen["name"] = name
        seen["wav"] = wav

    monkeypatch.setattr("src.core.modal_client.upload_voice", fake_upload)

    result = await ActivityEnvironment().run(
        voice_activities.upload_voice_activity, {"name": "anchor_neutral_m"}
    )

    assert result == {"name": "anchor_neutral_m", "uploaded": True}
    assert seen["name"] == "anchor_neutral_m"
    assert seen["wav"] == WAV


async def test_upload_voice_activity_rejects_missing_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))

    with pytest.raises(ApplicationError, match="no reference") as excinfo:
        await ActivityEnvironment().run(voice_activities.upload_voice_activity, {"name": "ghost"})
    assert excinfo.value.non_retryable is True


async def test_voice_catalog_status_activity_reports_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(
        voice_activities,
        "load_profiles",
        lambda: [
            voice_catalog.VoiceProfile.model_validate(_profile("a")),
            voice_catalog.VoiceProfile.model_validate(_profile("b")),
        ],
    )
    monkeypatch.setattr(voice_activities, "load_manifest", lambda: {})

    result = await ActivityEnvironment().run(voice_activities.voice_catalog_status_activity, {})

    assert result["generated"] == []
    assert sorted(result["missing"]) == ["a", "b"]
