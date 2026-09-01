"""Configuration settings for news-engine using pydantic-settings."""

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
