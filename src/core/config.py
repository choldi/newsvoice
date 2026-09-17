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
    api_port: int = Field(default=8000, ge=1, lt=65536)
    api_request_timeout: int = Field(default=120, ge=1)
    log_level: str = "INFO"
    database_path: str = "data/news.db"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "news-engine-task-queue"
    rss_feed_urls: list[str] = []
    collections_file: str = "collections.toml"
    tts_cache_dir: str = "data/tts_cache"
    tts_api_base_url: str = "http://192.168.5.4:8000/api/v1"
    tts_api_key: str = ""
    tts_language: str = "es"
    tts_voice: str = "es-mx-male"
    tts_max_chars: int = Field(default=5000, gt=0)
    tts_chunk_max_chars: int = Field(default=400, gt=0)
    tts_synthesis_concurrency: int = Field(default=2, ge=1)
    tts_podcast_behavior: str = ""
    tts_podcast_name: str = ""
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
