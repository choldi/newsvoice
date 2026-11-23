"""Pydantic models for the Phase 7.10 single-URL summary endpoint."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.models.narration import TTSBackend


class UrlSummarizeIn(BaseModel):
    """Request payload to summarize one article by URL."""

    url: str = Field(..., max_length=2048, description="Article URL to summarize (http/https)")
    collection: str = Field(
        default="default",
        description="Collection whose prompt and delivery destinations are used",
    )
    store: bool = Field(
        default=False,
        description="Persist the extracted article in `articles` (marks it as seen)",
    )
    deliver: bool = Field(
        default=True,
        description="Deliver the digest to the collection's configured channels",
    )
    language: str | None = Field(default=None, description="Language of the narrated summary")
    voice: str | None = Field(default=None, description="Voice id used to narrate the summary")
    tts_backend: str | None = Field(default=None, description="TTS backend used for narration")
    gateway_engine: str | None = Field(default=None, description="Gateway engine pin for narration")
    tts_enabled: bool = Field(default=False, description="Generate narration audio for the summary")
    deliver_audio: bool = Field(default=False, description="Deliver the generated audio")

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, value: str) -> str:
        """Accept only well-formed http(s) URLs.

        Args:
            value: Raw URL from the request body.

        Returns:
            The trimmed URL.

        Raises:
            ValueError: When the URL is blank, too long or not http(s).
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        if len(cleaned) > 2048:
            raise ValueError("url longer than 2048 characters")
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("url scheme must be http or https")
        if not parsed.netloc:
            raise ValueError("url must include a host")
        return cleaned

    @field_validator("collection")
    @classmethod
    def _collection_name(cls, value: str) -> str:
        """Trim the collection name; 'default' is used when blank.

        Args:
            value: Collection name from the request body.

        Returns:
            The trimmed name.
        """
        return value.strip() or "default"

    @field_validator("language", "voice", "tts_backend", "gateway_engine", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> str | None:
        """Normalize blank audio options to None.

        Args:
            value: Raw field value from JSON or the GUI form.

        Returns:
            The trimmed value, or None when empty.
        """
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @model_validator(mode="after")
    def _audio_options_are_consistent(self) -> UrlSummarizeIn:
        """Validate the backend and reject voices of a different engine.

        Returns:
            The validated payload.

        Raises:
            ValueError: If the backend is unknown or the voice visibly belongs
                to another engine's vocabulary.
        """
        from src.api.services.tts_backends import TTSBackendError, resolve_backend
        from src.api.services.voice_engine import VoiceEngineError, assert_voice_engine

        if not self.tts_backend:
            return self
        try:
            backend = resolve_backend(self.tts_backend)
        except TTSBackendError as exc:
            raise ValueError(str(exc)) from exc
        if backend is TTSBackend.GATEWAY:
            return self
        try:
            assert_voice_engine(self.voice or "", backend, field="voice")
        except VoiceEngineError as exc:
            raise ValueError(str(exc)) from exc
        return self


class UrlSummarizeOut(BaseModel):
    """Response payload: the run that will summarize the URL."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the URL summary run")
    collection: str = Field(..., description="Collection the run belongs to")
    url: str = Field(..., description="URL being summarized")
