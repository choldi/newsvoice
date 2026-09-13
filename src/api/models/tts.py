"""Pydantic models for text-to-speech generation."""

from pydantic import BaseModel, Field


class TTSIn(BaseModel):
    """Input payload to generate WAV audio from markdown text."""

    markdown: str = Field(..., min_length=1, description="Markdown text to synthesize")
    voice: str | None = Field(
        default=None, description="OmniVoice voice identifier (e.g. 'es-mx-male')"
    )
    language: str | None = Field(
        default=None, description="ISO 639-1 language code (e.g. 'es'). Defaults to settings."
    )


class TTSOut(BaseModel):
    """Response payload containing the audio file location and metadata."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    audio_path: str = Field(..., description="Path to the generated WAV file")
    duration_estimate: float = Field(..., description="Estimated audio duration in seconds")
    cached: bool = Field(..., description="Whether the audio was served from cache")
    voice: str = Field(..., description="TTS voice used for synthesis")
    char_count: int = Field(..., description="Number of characters synthesized")
