"""Tests for settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tts_api.settings import Settings, get_settings


class TestSettingsDefaults:
    """Tests for default settings values."""

    def test_app_name(self) -> None:
        settings = Settings()
        assert settings.APP_NAME == "TTS API"

    def test_app_version(self) -> None:
        # Los defaults no deben depender del .env local (que fija 0.2.0).
        settings = Settings(_env_file=None)
        assert settings.APP_VERSION == "0.1.0"

    def test_debug_default(self) -> None:
        settings = Settings()
        assert settings.DEBUG is False

    def test_api_prefix(self) -> None:
        settings = Settings()
        assert settings.API_PREFIX == "/api/v1"

    def test_tts_device(self) -> None:
        settings = Settings()
        assert settings.TTS_DEVICE == "cuda:0"

    def test_tts_dtype(self) -> None:
        settings = Settings()
        assert settings.TTS_DTYPE == "float16"

    def test_database_url(self) -> None:
        settings = Settings()
        assert "sqlite" in settings.DATABASE_URL

    def test_api_key_default_empty(self) -> None:
        settings = Settings()
        assert settings.API_KEY == ""

    def test_log_level_default(self) -> None:
        settings = Settings()
        assert settings.LOG_LEVEL == "INFO"

    def test_log_format_default(self) -> None:
        settings = Settings()
        assert settings.LOG_FORMAT == "json"

    def test_cors_origins_default(self) -> None:
        settings = Settings()
        assert settings.CORS_ORIGINS == "*"


class TestSettingsProperties:
    """Tests for computed properties."""

    def test_cors_origins_list_single(self) -> None:
        settings = Settings(CORS_ORIGINS="http://localhost:3000")
        assert settings.cors_origins_list == ["http://localhost:3000"]

    def test_cors_origins_list_multiple(self) -> None:
        settings = Settings(CORS_ORIGINS="http://a.com, http://b.com")
        assert settings.cors_origins_list == ["http://a.com", "http://b.com"]

    def test_cors_origins_list_wildcard(self) -> None:
        settings = Settings(CORS_ORIGINS="*")
        assert settings.cors_origins_list == ["*"]

    def test_cors_origins_list_strips_whitespace(self) -> None:
        settings = Settings(CORS_ORIGINS="  http://a.com ,  http://b.com  ")
        assert settings.cors_origins_list == ["http://a.com", "http://b.com"]

    def test_omnilang_list(self) -> None:
        settings = Settings()
        langs = settings.omnilang_list
        assert "es" in langs
        assert "en" in langs
        assert "zh" in langs
        assert len(langs) == 9


class TestSettingsValidation:
    """Tests for settings validation."""

    def test_invalid_log_level(self) -> None:
        with pytest.raises(ValidationError):
            Settings(LOG_LEVEL="INVALID")

    def test_invalid_log_format(self) -> None:
        with pytest.raises(ValidationError):
            Settings(LOG_FORMAT="xml")

    def test_invalid_omnivoice_dtype(self) -> None:
        with pytest.raises(ValidationError):
            Settings(TTS_DTYPE="bfloat16")


class TestNewEngineSettings:
    """Settings for the piper and chatterbox engines."""

    @pytest.mark.parametrize("engine", ["piper", "chatterbox"])
    def test_tts_engine_accepts_new_engines(self, engine: str) -> None:
        settings = Settings(TTS_ENGINE=engine)  # type: ignore[arg-type]
        assert engine == settings.TTS_ENGINE

    def test_tts_engine_rejects_unknown_engine(self) -> None:
        with pytest.raises(ValidationError):
            Settings(TTS_ENGINE="bogus")  # type: ignore[arg-type]

    def test_piper_defaults(self) -> None:
        settings = Settings()
        assert settings.PIPER_VOICE == "ca_ES-upc_ona-medium"
        assert settings.PIPER_USE_CUDA is False
        assert settings.PIPER_AUTO_DOWNLOAD is True

    def test_piper_voice_dir_override(self) -> None:
        settings = Settings(PIPER_VOICE="ca_ES-upc_pau-x_low", PIPER_USE_CUDA=True)
        assert settings.PIPER_VOICE == "ca_ES-upc_pau-x_low"
        assert settings.PIPER_USE_CUDA is True

    def test_chatterbox_defaults(self) -> None:
        settings = Settings()
        assert settings.CHATTERBOX_T3_MODEL == "v3"
        assert settings.CHATTERBOX_DEVICE == "cuda"
        assert settings.CHATTERBOX_EXAGGERATION == 0.5
        assert settings.CHATTERBOX_CFG_WEIGHT == 0.5

    def test_chatterbox_invalid_t3_model(self) -> None:
        with pytest.raises(ValidationError):
            Settings(CHATTERBOX_T3_MODEL="v9")


class TestGetSettings:
    """Tests for get_settings function."""

    def test_returns_settings_instance(self) -> None:
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_returns_new_instance_each_call(self) -> None:
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is not s2
