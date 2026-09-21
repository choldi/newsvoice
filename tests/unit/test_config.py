"""Unit tests for src/core/config.py."""

from src.core.config import Settings


def test_settings_default_values() -> None:
    """Tests default configuration settings values."""
    settings = Settings()
    assert settings.api_port == 8000
    assert settings.llm_temperature == 0.3
    assert settings.database_path == "data/news.db"
    assert settings.collections_file == "collections.toml"


def test_monitor_settings_defaults() -> None:
    """The system monitor has sensible defaults wired from env."""
    settings = Settings()
    assert settings.monitor_interval_minutes == 15
    assert settings.monitor_retention_days == 3
    assert settings.monitor_log_retention_days == 3
    assert settings.monitor_notify_channels == ["telegram"]
    assert settings.monitor_daily_summary is True
    assert settings.monitor_alert_on_run is False
    assert settings.monitor_disk_threshold_percent == 85
    assert settings.monitor_docker_socket == "/var/run/docker.sock"
    assert settings.monitor_tts_disk_path == "disk"
