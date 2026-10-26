"""Unit tests for the Phase 7.15 pronunciation respelling."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import NarrationRepository
from src.api.models.narration import NarrationScript, NarrationSpec, TTSBackend
from src.core.config import settings
from src.core.respelling import (
    apply_respelling,
    find_candidates,
    is_candidate,
    parse_respelling_json,
    respell_script,
    respell_text,
)

TEXT = "El startup de machine learning levanta funding con un chip nuevo."

SPEC: dict[str, Any] = {
    "title": "Tech",
    "tts_backend": "qwen3_tts",
    "roles": [
        {"name": "anchor", "voice_id": "es_fe_maria__qwen3_tts"},
        {"name": "guest", "voice_id": "es_ma_juan__qwen3_tts"},
    ],
}
SCRIPT: dict[str, Any] = {
    "source": "manual",
    "segments": [{"role": "anchor", "text": TEXT}],
}


def _script(spec: NarrationSpec) -> NarrationScript:
    return NarrationScript.model_validate(SCRIPT, context={"spec": spec})


def test_is_candidate_flags_english_terms_and_skips_native_words() -> None:
    assert is_candidate("startup")
    assert is_candidate("Apple")
    assert is_candidate("GPT-4")
    assert is_candidate("OpenAI")
    assert not is_candidate("para")
    assert not is_candidate("tecnología")


def test_find_candidates_keeps_order_and_drops_native_words() -> None:
    candidates = find_candidates("El startup de machine learning y el chip son clave.")

    assert candidates[:3] == ["startup", "machine", "learning"]
    assert "chip" in candidates
    assert "para" not in [candidate.lower() for candidate in candidates]


def test_apply_respelling_replaces_whole_words_only() -> None:
    result = apply_respelling(
        "El startup crece; los startups no. Startup otra vez.",
        {"startup": "estartap"},
    )

    assert result == "El estartap crece; los startups no. estartap otra vez."


def test_apply_respelling_prefers_the_longest_term() -> None:
    result = apply_respelling(
        "El machine learning y el machine solo.",
        {"machine": "mashin", "machine learning": "mashin lerning"},
    )

    assert result == "El mashin lerning y el mashin solo."


def test_apply_respelling_is_case_insensitive() -> None:
    assert apply_respelling("Apple y apple.", {"Apple": "apel"}) == "apel y apel."


def test_apply_respelling_ignores_identity_and_missing_terms() -> None:
    assert apply_respelling("El kernel.", {"kernel": "kernel", "cloud": "claud"}) == "El kernel."


def test_parse_respelling_json_handles_fences_and_prose() -> None:
    raw = 'Claro:\n```json\n{"startup": "estartap"}\n```'
    assert parse_respelling_json(raw) == {"startup": "estartap"}

    assert parse_respelling_json('Resultado: {"cloud": "claud"} listo.') == {"cloud": "claud"}


def test_parse_respelling_json_drops_invalid_entries() -> None:
    raw = '{"startup": "estartap", "cloud": 3, "chip": "", "k": "x"}'
    assert parse_respelling_json(raw) == {"startup": "estartap"}

    assert parse_respelling_json("not json") == {}


async def test_respell_text_asks_the_llm_in_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    mocked = AsyncMock(return_value='{"startup": "estartap", "chip": "chip"}')
    with patch("src.core.respelling.generate_completion", mocked):
        result = await respell_text(TEXT, "es")

    assert result.startswith("El estartap de machine learning")
    kwargs = mocked.call_args.kwargs
    assert kwargs["response_format"] == "json_object"
    assert kwargs["disable_thinking"] is True


async def test_respell_text_skips_the_llm_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", False)
    mocked = AsyncMock()
    with patch("src.core.respelling.generate_completion", mocked):
        assert await respell_text(TEXT, "es") == TEXT

    mocked.assert_not_called()


async def test_respell_text_skips_the_llm_without_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    mocked = AsyncMock()
    text = "Hoy hablamos de la economia y del gobierno."
    with patch("src.core.respelling.generate_completion", mocked):
        assert await respell_text(text, "es") == text

    mocked.assert_not_called()


async def test_respell_text_raises_when_the_llm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    with (
        patch(
            "src.core.respelling.generate_completion", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await respell_text(TEXT, "es")


async def test_respell_script_fills_and_caches_tts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    spec = NarrationSpec.model_validate(SPEC)
    mocked = AsyncMock(return_value='{"startup": "estartap"}')
    with patch("src.core.respelling.generate_completion", mocked):
        first = await respell_script(spec, _script(spec))

    assert first.segments[0].tts_text == (
        "El estartap de machine learning levanta funding con un chip nuevo."
    )
    assert first.segments[0].text == TEXT
    assert mocked.await_count == 1

    with patch("src.core.respelling.generate_completion", mocked):
        second = await respell_script(spec, first)

    assert second == first
    assert mocked.await_count == 1


async def test_respell_script_leaves_text_untouched_on_llm_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    spec = NarrationSpec.model_validate(SPEC)
    with patch("src.core.respelling.generate_completion", AsyncMock(side_effect=RuntimeError("x"))):
        result = await respell_script(spec, _script(spec))

    assert result.segments[0].tts_text is None
    assert result.segments[0].text == TEXT


async def test_respell_script_is_a_no_op_for_non_qwen_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "respelling_enabled", True)
    payload = {**SPEC, "tts_backend": TTSBackend.OMNIVOICE}
    spec = NarrationSpec.model_validate(payload)
    mocked = AsyncMock()
    with patch("src.core.respelling.generate_completion", mocked):
        result = await respell_script(spec, _script(spec))

    assert result.segments[0].tts_text is None
    mocked.assert_not_called()


async def test_resolve_activity_respells_and_persists_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.workers import narration_activities

    monkeypatch.setattr(settings, "respelling_enabled", True)
    spec = NarrationSpec.model_validate(SPEC)
    script = _script(spec)
    script_id = await NarrationRepository().save_script("run-respell", script, collection="tech")
    mocked = AsyncMock(return_value='{"startup": "estartap"}')

    with patch("src.core.respelling.generate_completion", mocked):
        # The admin trigger sends both the stored script and its id.
        resolved = await ActivityEnvironment().run(
            narration_activities.resolve_narration_script_activity,
            {
                "spec": SPEC,
                "script": script.model_dump(),
                "script_id": script_id,
                "collection": "tech",
            },
        )

    assert resolved["script"]["segments"][0]["tts_text"].startswith("El estartap")
    stored = await NarrationRepository().get_script(script_id)
    assert stored is not None
    assert stored.segments[0].tts_text == resolved["script"]["segments"][0]["tts_text"]
    assert stored.segments[0].text == TEXT
