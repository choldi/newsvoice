"""Unit tests for the Phase 7.15 pronunciation respelling."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import NarrationRepository
from src.api.models.narration import NarrationScript, NarrationSpec, TTSBackend
from src.api.models.phonetic import PhoneticHeuristicsIn
from src.api.services import phonetic_heuristics
from src.api.services.phonetic_heuristics import HeuristicsError
from src.core.config import settings
from src.core.respelling import (
    apply_respelling,
    builtin_heuristics,
    find_candidates,
    is_candidate,
    load_heuristics,
    parse_respelling_json,
    require_heuristics,
    respell_lesson_script,
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


# ---------------------------------------------------------------------------
# Phase 7.16: language-pair heuristics and the lesson adaptation
# ---------------------------------------------------------------------------


def _pair(language_origin: str, language_target: str, **overrides: object) -> PhoneticHeuristicsIn:
    """Build a valid heuristics payload for one test.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.
        **overrides: Field values replacing the defaults.

    Returns:
        A validated :class:`PhoneticHeuristicsIn`.
    """
    fields: dict = {
        "language_origin": language_origin,
        "language_target": language_target,
        "suffixes": [],
        "digraphs": [],
        "tech_terms": [],
        "native_words": [],
        "examples": {},
        "ascii_only": True,
        "flag_all_foreign": False,
        "enabled": True,
    }
    fields.update(overrides)
    return PhoneticHeuristicsIn.model_validate(fields)


LESSON_SPEC: dict[str, Any] = {
    "language": "en",
    "tts_backend": TTSBackend.QWEN3_TTS,
    "roles": [
        {"name": "profe_native", "voice_id": "voice-es", "language": "es"},
        {"name": "profe_target", "voice_id": "voice-en", "language": "en"},
    ],
}
LESSON_SCRIPT: dict[str, Any] = {
    "source": "manual",
    "language": "en",
    "segments": [
        {"role": "profe_native", "text": "En inglés decimos startup para una empresa nueva."},
        {"role": "profe_target", "text": "A startup is a young company."},
    ],
}


def _lesson_spec(**overrides: Any) -> NarrationSpec:
    """Build the dual cast used by the lesson adaptation tests."""
    return NarrationSpec.model_validate({**LESSON_SPEC, **overrides})


def _lesson_script(spec: NarrationSpec) -> NarrationScript:
    """Build the two-role lesson script used by the adaptation tests."""
    return NarrationScript.model_validate(LESSON_SCRIPT, context={"spec": spec})


def test_builtin_heuristics_only_cover_english_terms() -> None:
    """Only English origin rules live in code; everything else needs a row."""
    assert builtin_heuristics("en", "es") is not None
    assert builtin_heuristics("en", "ca") is not None, "Catalan targets are named now"
    assert builtin_heuristics("fr", "es") is None
    assert builtin_heuristics("en", "xx") is None, "an unknown target stays unsupported"


async def test_load_heuristics_prefers_the_database_row() -> None:
    """A stored row wins over the Phase 7.15 constants for the same pair."""
    seed = await phonetic_heuristics.get_pair("en", "es")
    assert seed is not None, "the migration seeds the narration pair"
    await phonetic_heuristics.save(
        _pair(
            "en",
            "es",
            suffixes=["zzz"],
            tech_terms=["startup"],
            native_words=["el"],
            examples={"startup": "estartap"},
            language_origin_name="English (db)",
        ),
        row_id=seed.id,
    )

    rules = await load_heuristics("en", "es")

    assert rules is not None
    assert rules.source == "db"
    assert rules.suffixes == ("zzz",)
    assert rules.language_origin_name == "English (db)"
    assert rules.examples == (("startup", "estartap"),)


async def test_load_heuristics_honours_a_disabled_row() -> None:
    """Disabling a pair in the GUI turns its respelling off for good."""
    await phonetic_heuristics.save(_pair("en", "ca", enabled=False))

    assert await load_heuristics("en", "ca") is None


async def test_load_heuristics_falls_back_to_the_builtin_pair() -> None:
    """No row and no override: the built-in defaults answer (narration)."""
    rules = await load_heuristics("en", "ca")

    assert rules is not None
    assert rules.source == "builtin"
    assert rules.language_target_name == "Catalan"


async def test_require_heuristics_reports_an_unconfigured_pair() -> None:
    """The lesson path never guesses: a missing row is an error."""
    with pytest.raises(HeuristicsError, match="no phonetic heuristics for de -> es"):
        await require_heuristics("de", "es")


async def test_require_heuristics_reads_the_french_seed() -> None:
    """The seeded lesson pair loads with its few-shot example attached."""
    rules = await require_heuristics("fr", "es")

    assert rules.source == "db"
    assert rules.flag_all_foreign is True
    assert rules.examples[0] == ("bonjour", "bonjur")


async def test_candidates_follow_the_pair_rules() -> None:
    """French rules see accents and short words; English rules do not."""
    french = await load_heuristics("fr", "es")

    assert french is not None
    assert is_candidate("bonjour", heuristics=french)
    assert is_candidate("café", heuristics=french), "ascii_only is off for French"
    assert not is_candidate("empresa", heuristics=french), "native words never reach the LLM"
    assert is_candidate("startup"), "the built-in English defaults are unchanged"
    assert not is_candidate("tecnología"), "and they still skip accented Spanish"


async def test_find_candidates_returns_the_pair_view() -> None:
    """A French quotation inside Spanish text is offered to the LLM."""
    french = await load_heuristics("fr", "es")

    assert french is not None
    candidates = find_candidates(
        "La cuisine, un bonjour y café por la mañana.", "es", heuristics=french
    )

    assert "bonjour" in candidates
    assert "café" in candidates
    assert "cuisine" in candidates
    assert "la" not in [candidate.lower() for candidate in candidates]


async def test_respell_text_uses_the_rules_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt names the pair's languages and applies the returned map."""
    monkeypatch.setattr(settings, "respelling_enabled", True)
    rules = await require_heuristics("fr", "es")
    mocked = AsyncMock(return_value='{"bonjour": "bonjur"}')

    with patch("src.core.respelling.generate_completion", mocked):
        result = await respell_text(
            "Hola, hoy decimos bonjour por la mañana.", "es", heuristics=rules
        )

    assert result == "Hola, hoy decimos bonjur por la mañana."
    kwargs = mocked.call_args.kwargs
    system = kwargs["messages"][0]["content"]
    assert "French" in system and "Spanish" in system
    assert kwargs["temperature"] == settings.respelling_temperature


async def test_respell_lesson_adapts_only_the_native_role_in_dual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dual mode the target-language role speaks natively: no adaptation."""
    monkeypatch.setattr(settings, "teacher_respelling_temperature", 0.55)
    spec = _lesson_spec()
    mocked = AsyncMock(return_value='{"startup": "estartap"}')

    with patch("src.core.respelling.generate_completion", mocked):
        adapted, metadata = await respell_lesson_script(
            _lesson_script(spec),
            spec,
            mode="dual",
            target_language="en",
            native_language="es",
        )

    assert adapted.segments[0].tts_text == ("En inglés decimos estartap para una empresa nueva.")
    assert adapted.segments[0].text.startswith("En inglés decimos startup"), "text is canonical"
    assert adapted.segments[1].tts_text is None
    assert mocked.await_count == 1, "only the native-language role asks for a map"
    assert mocked.call_args.kwargs["temperature"] == 0.55, "the teacher temperature wins"
    assert metadata["language_origin"] == "en"
    assert metadata["language_target"] == "es"
    assert metadata["terms"] == {"startup": "estartap"}
    entry = metadata["segments"][0]
    assert entry["role"] == "profe_native"
    assert entry["segment_index"] == 0
    assert entry["term_map"] == {"startup": "estartap"}
    assert "startup" in entry["original_text"]
    assert "estartap" in entry["respelled_text"]


async def test_respell_lesson_adapts_every_segment_in_mixed_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single teacher quotes the target language: every segment adapts."""
    monkeypatch.setattr(settings, "teacher_respelling_temperature", 0.2)
    spec = _lesson_spec(roles=[{"name": "teacher", "voice_id": "voice-es"}])  # type: ignore[arg-type]
    script = NarrationScript.model_validate(
        {
            "source": "manual",
            "segments": [
                {"role": "teacher", "text": "En inglés decimos startup a esto."},
                {"role": "teacher", "text": "Otra frase con startup y más ejemplos."},
            ],
        },
        context={"spec": spec},
    )
    mocked = AsyncMock(return_value='{"startup": "estartap"}')

    with patch("src.core.respelling.generate_completion", mocked):
        adapted, metadata = await respell_lesson_script(
            script, spec, mode="mixed", target_language="en", native_language="es"
        )

    assert [segment.tts_text for segment in adapted.segments] == [
        "En inglés decimos estartap a esto.",
        "Otra frase con estartap y más ejemplos.",
    ]
    assert mocked.await_count == 2
    assert metadata["terms"] == {"startup": "estartap"}
    assert len(metadata["segments"]) == 2


async def test_respell_lesson_rejects_equal_languages() -> None:
    """There is nothing to adapt when both languages are the same."""
    spec = _lesson_spec()

    with pytest.raises(HeuristicsError, match="different target and native"):
        await respell_lesson_script(
            _lesson_script(spec), spec, mode="dual", target_language="es", native_language="es"
        )


async def test_respell_lesson_raises_when_the_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No continue-on-error for a class: the failure must surface."""
    monkeypatch.setattr(settings, "teacher_respelling_temperature", 0.2)
    spec = _lesson_spec()

    with (
        patch(
            "src.core.respelling.generate_completion",
            AsyncMock(side_effect=RuntimeError("llm down")),
        ),
        pytest.raises(RuntimeError, match="llm down"),
    ):
        await respell_lesson_script(
            _lesson_script(spec), spec, mode="dual", target_language="en", native_language="es"
        )


async def test_respell_lesson_rejects_a_dual_cast_without_a_native_role() -> None:
    """A dual spec that forgets `language` would adapt nothing: refuse it."""
    spec = _lesson_spec(
        roles=[
            {"name": "teacher_a", "voice_id": "voice-es"},
            {"name": "teacher_b", "voice_id": "voice-en"},
        ]
    )
    script = NarrationScript.model_validate(
        {"source": "manual", "segments": [{"role": "teacher_a", "text": "Hola"}]},
        context={"spec": spec},
    )

    with pytest.raises(HeuristicsError, match="declares no 'es'-language role"):
        await respell_lesson_script(
            script, spec, mode="dual", target_language="en", native_language="es"
        )
