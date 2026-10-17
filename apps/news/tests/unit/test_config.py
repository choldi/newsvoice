"""Unit tests for src/core/config.py."""

import pytest
from pydantic import ValidationError

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
    assert settings.monitor_disk_threshold_percent == 20
    assert settings.monitor_docker_socket == "/var/run/docker.sock"
    assert settings.monitor_tts_disk_path == "disk"


def test_narration_base_url_falls_back_to_tts_gateway() -> None:
    """An empty narration URL reuses the TTS gateway, never our own API."""
    settings = Settings(
        narration_api_base_url="",
        tts_api_base_url="http://gateway.example:8001/api/v1",
    )
    assert settings.narration_base_url == "http://gateway.example:8001/api/v1"


def test_narration_base_url_accepts_explicit_gateway() -> None:
    """An explicit narration URL on the gateway port is allowed."""
    settings = Settings(
        narration_api_base_url="http://gateway.example:8001/api/v1",
        tts_api_base_url="http://gateway.example:8001/api/v1",
    )
    assert settings.narration_base_url == "http://gateway.example:8001/api/v1"


def test_narration_base_url_rejects_own_api_port() -> None:
    """Pointing the narration cast at the API port fails at startup.

    The news-engine API has no /tts route, so this misconfiguration used to
    surface as a runtime `NarrationGatewayError: Not Found` on every render.
    """
    with pytest.raises(ValidationError, match="news-engine API port"):
        Settings(
            narration_api_base_url="http://debwk.elver-exponential.ts.net:8000/api/v1",
            tts_api_base_url="http://debwk.elver-exponential.ts.net:8001/api/v1",
        )


def test_voxcpm2_settings_default_to_a_gated_best_of_three() -> None:
    """VoxCPM2 ships off, on the model card's sampling, keeping 3 takes."""
    settings = Settings()

    assert settings.voxcpm2_enabled is False
    assert settings.voxcpm2_model == "openbmb/VoxCPM2"
    assert settings.voxcpm2_cfg_value == 2.0
    assert settings.voxcpm2_inference_timesteps == 10
    assert settings.voxcpm2_candidates == 3


def test_voxcpm2_candidates_is_bounded() -> None:
    """An absurd take count is rejected instead of multiplying the spend."""
    with pytest.raises(ValidationError, match="voxcpm2_candidates"):
        Settings(voxcpm2_candidates=0)
    with pytest.raises(ValidationError, match="voxcpm2_candidates"):
        Settings(voxcpm2_candidates=99)


def test_narration_base_url_allows_shared_port_when_tts_also_there() -> None:
    """A shared port is legitimate when TTS and narration use the same host:port."""
    settings = Settings(
        narration_api_base_url="http://gateway.example:8000/api/v1",
        tts_api_base_url="http://gateway.example:8000/api/v1",
    )
    assert settings.narration_base_url == "http://gateway.example:8000/api/v1"
