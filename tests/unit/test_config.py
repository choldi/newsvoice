"""Unit tests for src/core/config.py."""

from src.core.config import Settings


def test_settings_default_values() -> None:
    """Tests default configuration settings values."""
    settings = Settings()
    assert settings.api_port == 8000
    assert settings.llm_temperature == 0.3
    assert settings.database_path == "data/news.db"
    assert settings.collections_file == "collections.toml"
