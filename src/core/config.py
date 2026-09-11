"""Configuration settings for news-engine using pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    llm_api_base: str = "http://localhost:8080/v1"
    llm_model: str = "openai/local-model"
    llm_temperature: float = 0.3
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    api_port: int = 8000
    log_level: str = "INFO"
    database_path: str = "data/news.db"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "news-engine-task-queue"
    tts_cache_dir: str = "data/tts_cache"
    tts_voice: str = "es-ES-ElviraNeural"
    tts_max_chars: int = 5000

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
