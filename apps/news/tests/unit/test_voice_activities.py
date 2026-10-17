"""Unit tests for the voice catalog activities (Phase 7.8/7.9)."""

import gzip
import json
import zlib
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.api.services import voice_staging
from src.core import voice_catalog
from src.core.audio import CANONICAL_FORMAT, build_wav, wav_duration
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


def _design_result(profile: dict[str, Any], wav: bytes = WAV) -> dict[str, Any]:
    """Build what ``design_voice_activity`` would return for a profile."""
    return {
        "name": profile["name"],
        "sha256": voice_catalog.sha256_bytes(wav),
        "sample_rate": 24000,
        "duration_seconds": wav_duration(wav),
        "design_model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "design_seconds": 1.5,
    }


def _stage(name: str, wav: bytes = WAV) -> None:
    """Stage audio the way the design activity would leave it."""
    voice_staging.write_staged(name, gzip.compress(wav))


def _store_payload(profile: dict[str, Any], wav: bytes = WAV) -> dict[str, Any]:
    """Build the store activity input for a profile and a design result."""
    _stage(profile["name"], wav)
    return {
        "name": profile["name"],
        "instruct": profile["instruct"],
        "ref_text": profile["ref_text"],
        **_design_result(profile, wav),
    }


# --- design (runs on the GPU box) -------------------------------------------


async def test_design_voice_activity_ships_the_audio_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_design(
        segments: list[str], instruct: str, language: str, seed: int | None = None
    ) -> tuple[bytes, int]:
        seen["segments"] = segments
        seen["instruct"] = instruct
        seen["language"] = language
        return WAV, 24000

    async def fake_stage(name: str, wav: bytes) -> int:
        seen["staged"] = (name, wav)
        return len(wav)

    monkeypatch.setattr("src.core.local_tts.design_voice", fake_design)
    monkeypatch.setattr("src.api.services.voice_staging.stage_from_design", fake_stage)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))

    result = await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())

    assert seen["instruct"] == _profile()["instruct"]
    assert seen["language"] == "en"
    assert seen["segments"] == [_profile()["ref_text"]]
    assert seen["staged"] == ("anchor_neutral_m", WAV)
    assert result["name"] == "anchor_neutral_m"
    assert result["sample_rate"] == 24000
    assert result["sha256"] == voice_catalog.sha256_bytes(WAV)
    # the audio leaves through staging, never through the activity result
    assert "wav_gz_b64" not in result
    assert not (tmp_path / "voices").exists()


async def test_design_voice_activity_marks_an_unconfigured_staging_key_as_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_design(
        segments: list[str], instruct: str, language: str, seed: int | None = None
    ) -> tuple[bytes, int]:
        return WAV, 24000

    monkeypatch.setattr("src.core.local_tts.design_voice", fake_design)
    monkeypatch.setattr(settings, "voice_staging_api_key", "")

    with pytest.raises(ApplicationError, match="voice staging failed") as excinfo:
        await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())

    assert excinfo.value.non_retryable is True


async def test_design_voice_activity_keeps_transport_errors_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_design(
        segments: list[str], instruct: str, language: str, seed: int | None = None
    ) -> tuple[bytes, int]:
        return WAV, 24000

    async def failing_stage(name: str, wav: bytes) -> int:
        raise voice_staging.VoiceStagingError("connection reset")

    monkeypatch.setattr("src.core.local_tts.design_voice", fake_design)
    monkeypatch.setattr("src.api.services.voice_staging.stage_from_design", failing_stage)

    with pytest.raises(ApplicationError, match="voice staging failed") as excinfo:
        await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())

    assert excinfo.value.non_retryable is False


async def test_design_voice_activity_forwards_segments_and_a_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_design(
        segments: list[str], instruct: str, language: str, seed: int | None = None
    ) -> tuple[bytes, int]:
        seen["segments"] = segments
        seen["seed"] = seed
        return WAV, 24000

    async def fake_stage(name: str, wav: bytes) -> int:
        return len(wav)

    monkeypatch.setattr("src.core.local_tts.design_voice", fake_design)
    monkeypatch.setattr("src.api.services.voice_staging.stage_from_design", fake_stage)
    payload = {**_profile(), "ref_segments": ["first half", "second half"]}

    await ActivityEnvironment().run(voice_activities.design_voice_activity, payload)

    assert seen["segments"] == ["first half", "second half"]
    assert seen["seed"] == zlib.crc32(b"qwen-voice-design:anchor_neutral_m")


async def test_design_voice_activity_rejects_incomplete_payload() -> None:
    with pytest.raises(ApplicationError, match="needs name, instruct and ref_text") as excinfo:
        await ActivityEnvironment().run(
            voice_activities.design_voice_activity, {"name": "anchor_neutral_m"}
        )
    assert excinfo.value.non_retryable is True


async def test_design_voice_activity_wraps_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_design(
        segments: list[str], instruct: str, language: str, seed: int | None = None
    ) -> tuple[bytes, int]:
        raise RuntimeError("cuda out of memory")

    monkeypatch.setattr("src.core.local_tts.design_voice", failing_design)

    with pytest.raises(ApplicationError, match="voice design failed"):
        await ActivityEnvironment().run(voice_activities.design_voice_activity, _profile())


# --- store (runs on the server) ---------------------------------------------


async def test_store_voice_activity_writes_reference_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))

    result = await ActivityEnvironment().run(
        voice_activities.store_voice_activity, _store_payload(_profile())
    )

    assert result["name"] == "anchor_neutral_m"
    assert result["sample_rate"] == 24000
    assert result["duration_seconds"] == wav_duration(WAV)
    ref_path = tmp_path / "voices" / "refs" / "anchor_neutral_m.wav"
    assert ref_path.read_bytes() == WAV
    manifest = json.loads((tmp_path / "voices" / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["anchor_neutral_m"]
    assert entry["sha256"] == voice_catalog.sha256_bytes(WAV)
    assert entry["design_model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert entry["modal_cost_usd"] == 0.0
    assert entry["instruct_sha256"] == voice_catalog.sha256_text(_profile()["instruct"])
    # the staging area is consumed once the reference is real
    assert not voice_staging.staged_path("anchor_neutral_m").exists()


async def test_store_voice_activity_keeps_a_previous_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    other = _profile("cohost_warm_f")

    await ActivityEnvironment().run(
        voice_activities.store_voice_activity, _store_payload(_profile())
    )
    await ActivityEnvironment().run(voice_activities.store_voice_activity, _store_payload(other))

    manifest = json.loads((tmp_path / "voices" / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest) == ["anchor_neutral_m", "cohost_warm_f"]


async def test_store_voice_activity_rejects_a_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    payload = _store_payload(_profile())
    payload["sha256"] = "0" * 64

    with pytest.raises(ApplicationError, match="does not match the reported digest") as excinfo:
        await ActivityEnvironment().run(voice_activities.store_voice_activity, payload)

    assert excinfo.value.non_retryable is True
    assert not (tmp_path / "voices" / "refs" / "anchor_neutral_m.wav").exists()


async def test_store_voice_activity_rejects_missing_staged_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    payload = _store_payload(_profile())
    voice_staging.clear_staged("anchor_neutral_m")

    with pytest.raises(ApplicationError, match="is unavailable") as excinfo:
        await ActivityEnvironment().run(voice_activities.store_voice_activity, payload)

    assert excinfo.value.non_retryable is True


async def test_store_voice_activity_rejects_corrupt_staged_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    payload = _store_payload(_profile())
    voice_staging.write_staged("anchor_neutral_m", b"not gzip at all")

    with pytest.raises(ApplicationError, match="is unavailable") as excinfo:
        await ActivityEnvironment().run(voice_activities.store_voice_activity, payload)

    assert excinfo.value.non_retryable is True


async def test_store_voice_activity_rejects_incomplete_payload() -> None:
    with pytest.raises(ApplicationError, match="needs name, instruct, ref_text") as excinfo:
        await ActivityEnvironment().run(voice_activities.store_voice_activity, {"name": "a"})
    assert excinfo.value.non_retryable is True


# --- upload -----------------------------------------------------------------


async def test_upload_voice_activity_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_dir = tmp_path / "voices" / "refs"
    ref_dir.mkdir(parents=True)
    (ref_dir / "anchor_neutral_m.wav").write_bytes(WAV)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    seen: dict[str, Any] = {}

    async def fake_upload(name: str, wav: bytes, ref_text: str = "") -> None:
        seen["name"] = name
        seen["wav"] = wav
        seen["ref_text"] = ref_text

    monkeypatch.setattr("src.core.modal_client.upload_voice", fake_upload)

    result = await ActivityEnvironment().run(
        voice_activities.upload_voice_activity,
        {"name": "anchor_neutral_m", "ref_text": "hola mundo"},
    )

    assert result == {"name": "anchor_neutral_m", "uploaded": True}
    assert seen["name"] == "anchor_neutral_m"
    assert seen["wav"] == WAV
    assert seen["ref_text"] == "hola mundo"


async def test_upload_voice_activity_rejects_missing_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))

    with pytest.raises(ApplicationError, match="no reference") as excinfo:
        await ActivityEnvironment().run(voice_activities.upload_voice_activity, {"name": "ghost"})
    assert excinfo.value.non_retryable is True


# --- status -----------------------------------------------------------------


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
