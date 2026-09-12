"""Unit tests for src/core/prompts.py."""

from src.core.prompts import (
    EMOTION_TAGS,
    SUMMARIZE_DIGEST_SYSTEM_PROMPT,
    build_digest_system_prompt,
    strip_emotion_tags,
)


def test_build_digest_system_prompt_no_behavior() -> None:
    """build_digest_system_prompt returns the base prompt for empty behavior."""
    assert build_digest_system_prompt("") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt("   ") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt(None) == SUMMARIZE_DIGEST_SYSTEM_PROMPT


def test_build_digest_system_prompt_with_behavior() -> None:
    """build_digest_system_prompt embeds the behavior and lists emotion tags."""
    prompt = build_digest_system_prompt("eres un podcaster, usa emociones cuando aplique")
    assert "eres un podcaster, usa emociones cuando aplique" in prompt
    assert "Additional instruction" in prompt
    for tag in EMOTION_TAGS:
        assert tag in prompt


def test_emotion_tags_match_spec() -> None:
    """EMOTION_TAGS matches the OmniVoice supported emotion list."""
    assert EMOTION_TAGS == frozenset(
        {"happy", "sad", "angry", "excited", "calm", "nervous", "whisper", "singing"}
    )


def test_strip_emotion_tags_removes_all_supported() -> None:
    """strip_emotion_tags removes supported emotion tags."""
    text = "[happy] Que alegria. [whisper] Un suspiro. [singing] Una cancion."
    cleaned = strip_emotion_tags(text)
    assert "[happy]" not in cleaned
    assert "[whisper]" not in cleaned
    assert "[singing]" not in cleaned
    assert "Que alegria" in cleaned


def test_strip_emotion_tags_keeps_unknown_brackets() -> None:
    """strip_emotion_tags does not remove non-emotion brackets."""
    text = "[calm] Nota con [nota] rara"
    cleaned = strip_emotion_tags(text)
    assert "nota" in cleaned
