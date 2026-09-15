"""Unit tests for src/core/prompts.py."""

from src.core.prompts import (
    DIGEST_TEMPLATES,
    EMOTION_TAGS,
    SUMMARIZE_DIGEST_SYSTEM_PROMPT,
    build_digest_system_prompt,
    get_digest_template,
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


def test_build_digest_system_prompt_with_language_only() -> None:
    """build_digest_system_prompt adds the language requirement without behavior."""
    prompt = build_digest_system_prompt(None, language="en")
    assert "Language requirement" in prompt
    assert "Write the ENTIRE digest in the language code above." in prompt
    assert "en" in prompt
    assert "Additional instruction" not in prompt


def test_build_digest_system_prompt_with_language_and_behavior() -> None:
    """build_digest_system_prompt combines both the behavior and the language."""
    prompt = build_digest_system_prompt("eres un podcaster", language="es")
    assert "Additional instruction" in prompt
    assert "eres un podcaster" in prompt
    assert "Language requirement" in prompt
    assert "es" in prompt


def test_build_digest_system_prompt_ignores_blank_language() -> None:
    """build_digest_system_prompt ignores blank/None language, returning base prompt."""
    assert build_digest_system_prompt(None, language="  ") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt(None, language=None) == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt("", language="") == SUMMARIZE_DIGEST_SYSTEM_PROMPT


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


def test_get_digest_template_default() -> None:
    """Unknown or blank template names fall back to the base prompt."""
    assert get_digest_template(None) == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert get_digest_template("") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert get_digest_template("does-not-exist") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert get_digest_template("default") == SUMMARIZE_DIGEST_SYSTEM_PROMPT


def test_get_digest_template_known() -> None:
    """Named templates are returned and build on the base prompt."""
    finance = get_digest_template("finance")
    assert finance is not SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert "Finance-specific requirements" in finance
    assert {"finance", "health", "investigative"} <= set(DIGEST_TEMPLATES)


def test_build_digest_system_prompt_with_template() -> None:
    """build_digest_system_prompt embeds the requested template."""
    prompt = build_digest_system_prompt(None, template="finance")
    assert "Finance-specific requirements" in prompt
    # The base prompt mentions Markdown only once; the finance suffix adds a
    # second mention via the "Keep the Markdown structure" instruction.
    assert prompt.count("Keep the Markdown") == 1


def test_build_digest_system_prompt_template_and_behavior() -> None:
    """Template, behavior, and language compose into one system prompt."""
    prompt = build_digest_system_prompt("eres un podcaster", language="es", template="health")
    assert "Health-specific requirements" in prompt
    assert "eres un podcaster" in prompt
    assert "Language requirement" in prompt


def test_build_digest_system_prompt_ignores_blank_podcast_name() -> None:
    """Blank/None podcast_name leaves the base prompt unchanged."""
    assert build_digest_system_prompt(None, podcast_name=None) == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt(None, podcast_name="  ") == SUMMARIZE_DIGEST_SYSTEM_PROMPT
    assert build_digest_system_prompt("", podcast_name="") == SUMMARIZE_DIGEST_SYSTEM_PROMPT


def test_build_digest_system_prompt_with_podcast_name() -> None:
    """podcast_name adds the podcast identity block to the system prompt."""
    prompt = build_digest_system_prompt(None, podcast_name="ByteCast")
    assert "Podcast identity" in prompt
    assert "This is ByteCast" in prompt
    assert "co-host/anchor of the news podcast" in prompt
    assert "Additional instruction" not in prompt


def test_build_digest_system_prompt_with_podcast_name_and_behavior() -> None:
    """podcast_name composes with behavior and language sections."""
    prompt = build_digest_system_prompt(
        "usa un tono fresco", language="es", podcast_name="ByteCast"
    )
    assert "Podcast identity" in prompt
    assert "This is ByteCast" in prompt
    assert "usa un tono fresco" in prompt
    assert "Language requirement" in prompt


def test_build_digest_system_prompt_custom_overrides_template() -> None:
    """A non-blank custom_prompt replaces the built-in template entirely."""
    custom = "You are a financial analyst. Focus exclusively on market risk."
    prompt = build_digest_system_prompt(None, template="finance", custom_prompt=custom)
    assert "Finance-specific requirements" not in prompt
    assert custom in prompt


def test_build_digest_system_prompt_custom_ignores_blank() -> None:
    """Blank/None custom_prompt falls back to the template."""
    base = build_digest_system_prompt(None, template="finance")
    assert base == build_digest_system_prompt(None, template="finance", custom_prompt="")
    assert base == build_digest_system_prompt(None, template="finance", custom_prompt=None)
    assert base == build_digest_system_prompt(None, template="finance", custom_prompt="   ") == base
