"""Pydantic models for text-to-speech generation."""

from pydantic import BaseModel, Field


class TTSIn(BaseModel):
    """Input payload to generate MP3 audio from markdown text."""

    markdown: str = Field(
        ...,
        min_length=1,
        # Hard ceiling on the request body; longer digests are still truncated to
        # TTS_MAX_CHARS by the service, so this only bounds abusive payloads.
        max_length=20_000,
        description="Markdown text to synthesize (truncated to TTS_MAX_CHARS)",
    )
    voice: str | None = Field(
        default=None,
        description=(
            "Edge neural voice ShortName (e.g. 'es-MX-JorgeNeural') or a legacy "
            "alias such as 'es-mx-male'."
        ),
    )
    language: str | None = Field(
        default=None, description="ISO 639-1 language code (e.g. 'es'). Defaults to settings."
    )
    collection: str = Field(
        default="default",
        description="Collection name used to name the output file "
        "news_<collection>_<yymmdd>.<ext>.",
    )


class TTSOut(BaseModel):
    """Response payload containing the audio file location and metadata."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    audio_path: str = Field(..., description="Path to the generated MP3 file")
    duration_estimate: float = Field(..., description="Estimated audio duration in seconds")
    cached: bool = Field(..., description="Whether the audio was served from cache")
    voice: str = Field(..., description="TTS voice used for synthesis")
    char_count: int = Field(..., description="Number of characters synthesized")
