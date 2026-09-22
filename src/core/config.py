"""Configuration settings for news-engine using pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    llm_api_base: str = "http://localhost:8080/v1"
    llm_model: str = "openai/local-model"
    llm_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    llm_timeout_seconds: int = Field(default=1800, ge=1)
    llm_max_tokens: int = Field(default=4096, gt=0)
    llm_disable_thinking: bool = True
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, lt=65536)
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_from_name: str = "News Engine"
    # WhatsApp delivery via a self-hosted Baileys gateway (docker-compose
    # service named "there"; blakegt/whatsapp-web-api-rest listening on :8085).
    # Fail-closed: with no api_token configured the channel is disabled and a
    # digest never blocks on it; numbers must be in the allowlist (groups are
    # always allowed).
    whatsapp_api_url: str = "http://there:8085"
    whatsapp_api_token: str = ""
    whatsapp_allowlist: list[str] = Field(
        default_factory=list,
        description="E.164 numbers allowed to receive digests (groups exempt)",
    )
    # OpenClaw-style human typing delay between sends:
    # "off"=none, "natural"=800-2500ms, "custom"=[custom_min, custom_max].
    whatsapp_delay_mode: str = "natural"
    whatsapp_delay_custom_min_ms: int = Field(default=2000, ge=0)
    whatsapp_delay_custom_max_ms: int = Field(default=8000, ge=0)
    # Typing indicator while 'writing': "composing" or "none".
    whatsapp_typing_indicator: str = "composing"
    # Minimum cool-down per recipient between consecutive sends (milliseconds).
    whatsapp_recipient_cooldown_ms: int = Field(default=300, ge=0)
    api_port: int = Field(default=8000, ge=1, lt=65536)
    api_request_timeout: int = Field(default=120, ge=1)
    log_level: str = "INFO"
    database_path: str = "data/news.db"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "news-engine-task-queue"
    rss_feed_urls: list[str] = []
    collections_file: str = "collections.toml"
    # How often (minutes) the collections-config monitor workflow wakes to check
    # whether collections.toml changed and re-register the Temporal schedules.
    collections_monitor_interval_minutes: int = Field(default=5, ge=1)
    # How often (minutes) the system-monitor workflow wakes to run the checks.
    monitor_interval_minutes: int = Field(default=15, ge=1)
    # Retention window (days) for monitor events in SQLite and JSONL log files.
    monitor_retention_days: int = Field(default=3, ge=1)
    monitor_log_retention_days: int = Field(default=3, ge=1)
    # Directory where the daily-rotated JSONL monitor logs are written.
    monitor_log_dir: str = "data/logs"
    # Alert delivery for the system monitor. Defaults to Telegram using
    # monitor_telegram_chat_id (or the global telegram_chat_id). Supported
    # channels: telegram, email, whatsapp. Empty list disables alerts.
    monitor_notify_channels: list[str] = Field(default_factory=lambda: ["telegram"])
    monitor_telegram_chat_id: str = ""
    monitor_email_recipients: list[str] = Field(default_factory=list)
    monitor_whatsapp_recipients: list[str] = Field(default_factory=list)
    monitor_whatsapp_group_id: str | None = None
    # Emit an alert when free disk space drops below this percentage
    # (e.g. 20 = warn when the volume is over 80% full).
    monitor_disk_threshold_percent: int = Field(default=20, ge=1, le=99)
    # Send a daily system health summary (default) plus a per-run confirmation
    # when monitor_alert_on_run is enabled.
    monitor_daily_summary: bool = True
    monitor_alert_on_run: bool = False
    # TTS server disk probe: <tts_api_base_url>/<monitor_tts_disk_path> must
    # return {"total": int, "free": int, "used": int} in bytes.
    monitor_tts_disk_path: str = "disk"
    # Path to the Docker Engine UNIX socket used to probe container health.
    # Empty disables the docker_containers check.
    monitor_docker_socket: str = "/var/run/docker.sock"
    tts_cache_dir: str = "data/tts_cache"
    tts_api_base_url: str = "http://debwk.elver-exponential.ts.net:8000/api/v1"
    tts_api_key: str = ""
    tts_language: str = "es"
    tts_voice: str = "es-MX-JorgeNeural"
    tts_max_chars: int = Field(default=5000, gt=0)
    # Per-request timeout for a single Edge TTS synthesis call.
    tts_request_timeout: int = Field(default=600, ge=1)
    tts_podcast_behavior: str = ""
    tts_podcast_name: str = ""
    # Transcode the synthesized WAV to M4A (AAC) before delivery so the file is
    # small enough for Telegram's 50 MB limit and plays on Telegram, WhatsApp,
    # Android, and iPhone. ffmpeg is installed in the Docker image.
    ffmpeg_path: str = "ffmpeg"
    tts_audio_bitrate_kbps: int = Field(default=48, ge=16, le=320)
    admin_api_token: str = ""

    @property
    def data_dir(self) -> Path:
        """Return the data directory (parent of database file)."""
        return Path(self.database_path).parent

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return settings
