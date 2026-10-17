"""Tests for custom exceptions."""

from __future__ import annotations

import pytest

from tts_api.core.exceptions import (
    EngineUnavailableError,
    InvalidReferenceAudioError,
    OmniVoiceAPIError,
    UnsupportedInstructError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)


class TestOmniVoiceAPIError:
    """Tests for base exception class."""

    def test_is_subclass_of_exception(self) -> None:
        assert issubclass(OmniVoiceAPIError, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(OmniVoiceAPIError):
            raise OmniVoiceAPIError("test error")


class TestVoiceNotFoundError:
    """Tests for VoiceNotFoundError."""

    def test_attributes(self) -> None:
        err = VoiceNotFoundError("voice-123", "cloned")
        assert err.voice_id == "voice-123"
        assert err.voice_type == "cloned"

    def test_default_voice_type(self) -> None:
        err = VoiceNotFoundError("voice-123")
        assert err.voice_type == "stock"

    def test_valid_voice_ids_default_none(self) -> None:
        err = VoiceNotFoundError("voice-123")
        assert err.valid_voice_ids is None

    def test_message_contains_voice_id(self) -> None:
        err = VoiceNotFoundError("voice-123", "stock")
        assert "voice-123" in str(err)

    def test_inherits_from_base(self) -> None:
        assert issubclass(VoiceNotFoundError, OmniVoiceAPIError)


class TestUnsupportedLanguageError:
    """Tests for UnsupportedLanguageError."""

    def test_attributes(self) -> None:
        err = UnsupportedLanguageError("xx", ["es", "en", "fr"])
        assert err.language == "xx"
        assert err.supported_languages == ["es", "en", "fr"]

    def test_message_contains_language(self) -> None:
        err = UnsupportedLanguageError("xx", ["es", "en"])
        assert "xx" in str(err)

    def test_message_contains_supported(self) -> None:
        err = UnsupportedLanguageError("xx", ["es", "en"])
        assert "es" in str(err)
        assert "en" in str(err)

    def test_inherits_from_base(self) -> None:
        assert issubclass(UnsupportedLanguageError, OmniVoiceAPIError)


class TestUnsupportedInstructError:
    """Tests for UnsupportedInstructError."""

    def test_attributes(self) -> None:
        err = UnsupportedInstructError("bad token", {"bad token": "good token"}, ["good token"])
        assert err.instruct == "bad token"
        assert err.invalid_items == {"bad token": "good token"}
        assert err.valid_items == ["good token"]

    def test_message_with_suggestion(self) -> None:
        err = UnsupportedInstructError("bad", {"bad": "good"}, ["good"])
        assert "bad" in str(err)
        assert "good" in str(err)

    def test_message_without_suggestion(self) -> None:
        err = UnsupportedInstructError("bad", {"bad": None}, ["good"])
        assert "bad" in str(err)

    def test_inherits_from_base(self) -> None:
        assert issubclass(UnsupportedInstructError, OmniVoiceAPIError)


class TestEngineUnavailableError:
    """Tests for EngineUnavailableError."""

    def test_attributes(self) -> None:
        err = EngineUnavailableError("GPU not found")
        assert err.detail == "GPU not found"

    def test_default_detail(self) -> None:
        err = EngineUnavailableError()
        assert err.detail == "Motor de síntesis no disponible"

    def test_inherits_from_base(self) -> None:
        assert issubclass(EngineUnavailableError, OmniVoiceAPIError)


class TestInvalidReferenceAudioError:
    """Tests for InvalidReferenceAudioError."""

    def test_attributes(self) -> None:
        err = InvalidReferenceAudioError("Audio too short")
        assert err.detail == "Audio too short"

    def test_inherits_from_base(self) -> None:
        assert issubclass(InvalidReferenceAudioError, OmniVoiceAPIError)
