"""Unit tests for the Phase 7.14 narration render hook (audio verification)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import VerificationsRepository
from src.api.models.narration import (
    NarrationOut,
    NarrationScript,
    NarrationSpec,
    SpeakerRole,
)
from src.api.services.audio_verify import AudioVerification
from src.workers.narration_activities import (
    NARRATION_JOB_KIND,
    _expected_narration_text,
    render_narration_activity,
)

RUN_ID = "run-verify-1"


def _spec() -> NarrationSpec:
    """A minimal single-role cast on the local gateway."""
    return NarrationSpec(
        title="Clase",
        language="es",
        roles=[SpeakerRole(name="profe", voice_id="es-mx-male")],
    )


def _script() -> NarrationScript:
    """A two-segment script rendered by the fake service."""
    return NarrationScript(
        title="Clase",
        language="es",
        source="llm",
        segments=[
            {"role": "profe", "text": "Bienvenidos a clase de espanol."},
            {"role": "profe", "text": "Hoy practicamos el subjuntivo."},
        ],
    )


def _render_result(tmp_path: Path) -> NarrationOut:
    """A plausible render outcome pointing at a file on disk."""
    audio_path = tmp_path / "narration.m4a"
    audio_path.write_bytes(b"ID3fake")
    return NarrationOut(
        audio_path=str(audio_path),
        wav_path=str(tmp_path / "narration.wav"),
        duration_seconds=42.0,
        segments=2,
        char_count=64,
    )


def _verification(passed: bool = True, wer: float = 0.1) -> AudioVerification:
    """A canned ASR score."""
    return AudioVerification(
        wer=wer,
        similarity=round(1 - wer, 4),
        passed=passed,
        threshold=0.3,
        transcript="Bienvenidos a clase de espanol. Hoy practicamos el subjuntivo.",
        blocks=["Bienvenidos..."],
        block_count=1,
        duration_seconds=42.0,
        model="base",
        language="es",
        expected_chars=64,
        transcript_chars=64,
        verdict="valid" if passed else "invalid",
    )


def _payload(spec: NarrationSpec, script: NarrationScript, **extra: object) -> dict:
    """Build the render activity payload."""
    base: dict = {
        "run_id": RUN_ID,
        "collection": "tech",
        "spec": spec.model_dump(mode="json"),
        "script": script.model_dump(mode="json"),
        "strategy": "turns",
    }
    base.update(extra)
    return base


@pytest.fixture()
def environment() -> ActivityEnvironment:
    """An ActivityEnvironment providing the activity context."""
    return ActivityEnvironment()


async def test_render_without_the_opt_in_skips_verification(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """No audio_verify payload: the render returns and never calls the ASR."""
    result_mock = AsyncMock(return_value=_render_result(tmp_path))
    verify_mock = AsyncMock(return_value=_verification())

    with (
        patch("src.workers.narration_activities.NarrationTTSService") as service_cls,
        patch("src.workers.narration_activities.verify_audio", verify_mock),
    ):
        service_cls.return_value.render = result_mock
        payload = await environment.run(render_narration_activity, _payload(_spec(), _script()))

    assert payload["verification"] is None
    verify_mock.assert_not_awaited()


async def test_render_verifies_and_records_a_passing_score(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """enabled audio_verify transcribes, stores the verdict and returns it."""
    result_mock = AsyncMock(return_value=_render_result(tmp_path))
    verify_mock = AsyncMock(return_value=_verification(passed=True, wer=0.05))

    with (
        patch("src.workers.narration_activities.NarrationTTSService") as service_cls,
        patch("src.workers.narration_activities.verify_audio", verify_mock),
    ):
        service_cls.return_value.render = result_mock
        payload = await environment.run(
            render_narration_activity,
            _payload(_spec(), _script(), audio_verify={"enabled": True, "threshold": 0.3}),
        )

    verification = payload["verification"]
    assert verification["passed"] is True
    assert verification["score"] == 0.05
    assert verification["threshold"] == 0.3

    # The expected text handed to the ASR is the script itself.
    awaited = verify_mock.await_args
    expected_arg = awaited.args[1]
    assert "Bienvenidos a clase de espanol." in expected_arg
    assert "Hoy practicamos el subjuntivo." in expected_arg

    rows = await VerificationsRepository().get_for_run(RUN_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == "narration_audio"
    assert row["method"] == "asr_wer"
    assert row["passed"] is True
    assert row["score"] == 0.05
    assert row["threshold"] == 0.3


async def test_render_withholds_a_failing_score(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """A WER above the threshold is recorded as invalid and returned as such."""
    result_mock = AsyncMock(return_value=_render_result(tmp_path))
    verify_mock = AsyncMock(return_value=_verification(passed=False, wer=0.9))

    with (
        patch("src.workers.narration_activities.NarrationTTSService") as service_cls,
        patch("src.workers.narration_activities.verify_audio", verify_mock),
    ):
        service_cls.return_value.render = result_mock
        payload = await environment.run(
            render_narration_activity,
            _payload(_spec(), _script(), audio_verify={"enabled": True, "threshold": 0.3}),
        )

    verification = payload["verification"]
    assert verification["passed"] is False
    assert verification["verdict"] == "invalid"
    assert verification["score"] == 0.9

    rows = await VerificationsRepository().get_for_run(RUN_ID)
    assert rows[0]["passed"] is False
    assert rows[0]["verdict"] == "invalid"


async def test_render_records_an_error_verdict_when_asr_is_unusable(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """An unusable file fails closed: verdict 'error', audio withheld."""
    from src.api.services.audio_verify import AudioVerifyError

    result_mock = AsyncMock(return_value=_render_result(tmp_path))
    verify_mock = AsyncMock(side_effect=AudioVerifyError("audio file is missing"))

    with (
        patch("src.workers.narration_activities.NarrationTTSService") as service_cls,
        patch("src.workers.narration_activities.verify_audio", verify_mock),
    ):
        service_cls.return_value.render = result_mock
        payload = await environment.run(
            render_narration_activity,
            _payload(_spec(), _script(), audio_verify={"enabled": True, "threshold": 0.3}),
        )

    verification = payload["verification"]
    assert verification["passed"] is False
    assert verification["verdict"] == "error"
    assert "audio file is missing" in verification["reason"]

    rows = await VerificationsRepository().get_for_run(RUN_ID)
    assert rows[0]["verdict"] == "error"
    assert rows[0]["passed"] is False
    assert rows[0]["score"] is None


def test_expected_text_includes_the_ai_disclosure() -> None:
    """When the spec asks for it, the audible disclosure is part of the script."""
    from src.core.config import settings

    spec = _spec()
    spec.ai_disclosure = True
    script = _script()
    original = settings.narration_ai_disclosure_text
    try:
        settings.narration_ai_disclosure_text = "Este episodio es IA."
        expected = _expected_narration_text(spec, script)
    finally:
        settings.narration_ai_disclosure_text = original

    assert expected.startswith("Este episodio es IA.")
    assert "Hoy practicamos el subjuntivo." in expected
    assert NARRATION_JOB_KIND == "narration"
