"""Unit tests for src/core/prompts.py."""

from src.core.prompts import (
    DIGEST_TEMPLATE_ADDITIONS,
    EMOTION_TAGS,
    _build_base_digest_prompt,
    build_digest_system_prompt,
    get_digest_template,
    strip_emotion_tags,
)


def test_build_digest_system_prompt_no_behavior() -> None:
    """build_digest_system_prompt returns the base prompt for empty behavior."""
    base = _build_base_digest_prompt(5, 5)
    assert build_digest_system_prompt("") == base
    assert build_digest_system_prompt("   ") == base
    assert build_digest_system_prompt(None) == base


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
    base = _build_base_digest_prompt(5, 5)
    assert build_digest_system_prompt(None, language="  ") == base
    assert build_digest_system_prompt(None, language=None) == base
    assert build_digest_system_prompt("", language="") == base


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
    base = _build_base_digest_prompt(5, 5)
    assert get_digest_template(None) == base
    assert get_digest_template("") == base
    assert get_digest_template("does-not-exist") == base
    assert get_digest_template("default") == base


def test_get_digest_template_known() -> None:
    """Named templates are returned and build on the base prompt."""
    finance = get_digest_template("finance")
    assert finance is not _build_base_digest_prompt(5, 5)
    assert "Finance-specific requirements" in finance
    assert {"finance", "health", "investigative", "gaming"} <= set(DIGEST_TEMPLATE_ADDITIONS)


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


def test_build_digest_system_prompt_with_custom_prompt() -> None:
    """custom_prompt overrides template entirely when provided."""
    custom = "You are a custom editor. Write the digest in French."
    prompt = build_digest_system_prompt(
        "some behavior", language="es", template="finance", custom_prompt=custom
    )
    assert prompt == custom
    assert "Finance-specific" not in prompt


def test_get_digest_template_with_custom_counts() -> None:
    """get_digest_template respects the main_story_count and headline_count."""
    base_5_5 = get_digest_template("finance", 5, 5)
    base_5_10 = get_digest_template("finance", 5, 10)
    base_10_5 = get_digest_template("finance", 10, 5)

    assert "Top 5 Main Stories" in base_5_5
    assert "Top 5 Quick Headlines" in base_5_5
    assert "Top 5 Main Stories" in base_5_10
    assert "Top 10 Quick Headlines" in base_5_10
    assert "Top 10 Main Stories" in base_10_5
    assert "Top 5 Quick Headlines" in base_10_5
    # Domain-specific additions should be present in all
    for p in (base_5_5, base_5_10, base_10_5):
        assert "Finance-specific requirements" in p


def test_build_digest_system_prompt_preserves_template_with_counts() -> None:
    """build_digest_system_prompt preserves template additions when counts != 5/5."""
    prompt = build_digest_system_prompt(
        None, template="gaming", main_story_count=5, headline_count=10
    )
    assert "Top 5 Main Stories" in prompt
    assert "Top 10 Quick Headlines" in prompt
    assert "Gaming-specific requirements" in prompt
    assert "extended summaries of 5-6 sentences" in prompt


def test_build_digest_system_prompt_podcast_name() -> None:
    """build_digest_system_prompt injects podcast name when provided."""
    prompt = build_digest_system_prompt(None, podcast_name="My Podcast")
    assert "Podcast identity" in prompt
    assert "This is My Podcast" in prompt
