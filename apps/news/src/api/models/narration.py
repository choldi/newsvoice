"""Pydantic models for the multi-speaker narration pipeline (Phase 7).

A narration turns a digest into a scripted audio piece: an ordered list of
segments, each assigned to one of the roles of the cast, optionally laid over a
music bed. The models here are the domain contract shared by the renderer
(Phase 7.2), the Temporal activities (7.3) and the admin API (7.4); they
deliberately carry no I/O.

Field bounds mirror what the TTS gateway actually accepts
(`/api/v1/tts` and `/api/v1/conversations`): `speed` in [0.5, 2.0], `pause_ms`
in [0, 5000] and two-letter ISO 639-1 language codes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.models.voices import VoiceSource
from src.core.prompts import EMOTION_TAGS

# Audio jobs of a run are discriminated by this column ('tts' | 'narration' | 'audiobook').
NARRATION_KIND = "narration"
TTS_KIND = "tts"
AUDIOBOOK_KIND = "audiobook"
AUDIO_JOB_KINDS = (TTS_KIND, NARRATION_KIND, AUDIOBOOK_KIND)

# Where a script came from: the LLM, the admin editor/GUI, or an external API.
SCRIPT_SOURCES = ("llm", "manual", "api")


class TTSBackend(StrEnum):
    """Which engine synthesizes a narration or a digest.

    Attributes:
        EDGE: Client-side ``edge-tts`` (Microsoft neural voices). No gateway,
            no spend, no GPU. This is the digest default and the fallback the
            other engines degrade to.
        OMNIVOICE: The self-hosted TTS gateway (rebranded ``tts-api``, port
            8001): OmniVoice / pocket_tts / edge-tts routed per language. Local,
            no spend, honors per-turn speed and emotion.
        QWEN3_TTS: Qwen3-TTS on Modal. Synthetic voices from the catalog; no
            emotion or speed control (speed is post-processed with atempo).
        ZONOS2: ZONOS2 on Modal. Adds native speed and emotion control.
        VOXCPM2: VoxCPM2 on Modal (Apache-2.0, 2B, 48 kHz). Clones the catalog
            voice in audio-continuation mode and keeps the best of several
            takes by a no-reference MOS; no native speed or emotion control
            (speed is post-processed with atempo).
        KOKORO: Kokoro-82M via the tts-api gateway (stock voices only, CPU-first,
            24 kHz). No cloning/emotion/instruct; good for es/en/fr/it/pt/hi.
    """

    EDGE = "edge"
    OMNIVOICE = "omni_voice"
    QWEN3_TTS = "qwen3_tts"
    ZONOS2 = "zonos2"
    VOXCPM2 = "voxcpm2"
    KOKORO = "kokoro"


class SpeakerRole(BaseModel):
    """One voice in the narration cast.

    Attributes:
        name: Role name used by script segments (e.g. 'anchor', 'expert').
        voice_id: Gateway voice id (stock ShortName or cloned voice UUID).
        language: Optional ISO 639-1 override for this role.
        speed: Speaking rate in [0.5, 2.0].
        emotion: Optional emotion tag, honored only by OmniVoice backends.
        pitch_semitones: Pitch shift in semitones (-6..6), applied by rubberband
            post-processing (all backends). 0.0 = no change.
        formant_preserve: Preserve formants when shifting pitch (rubberband).
        variant: Optional variant label for this voice (for UI/cataloging).
    """

    name: str = Field(..., min_length=1, max_length=64, description="Role name used by segments")
    voice_id: str = Field(
        ..., min_length=1, max_length=128, description="Gateway voice id (stock name or clone UUID)"
    )
    language: str | None = Field(
        default=None,
        description="ISO 639-1 override for this role; defaults to the spec language",
    )
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Speaking rate multiplier")
    emotion: str | None = Field(
        default=None, description="Emotion tag (happy, calm, excited, ...); OmniVoice only"
    )
    pitch_semitones: float = Field(
        default=0.0, ge=-6.0, le=6.0, description="Pitch shift in semitones (-6..6)"
    )
    formant_preserve: bool = Field(
        default=True, description="Preserve formants when applying pitch shift"
    )
    variant: str | None = Field(default=None, max_length=64, description="Voice variant label")

    @field_validator("language")
    @classmethod
    def _language_is_iso639_1(cls, value: str | None) -> str | None:
        """Keep only two-letter lowercase language codes.

        The gateway rejects anything longer (e.g. 'es-MX' or 'en-GB') with a
        400 unsupported_language, so fail early with a clear message.

        Args:
            value: Language code supplied for the role.

        Returns:
            The lowercased code, or None when unset.

        Raises:
            ValueError: If the code is not a two-letter ISO 639-1 code.
        """
        if value is None:
            return None
        code = value.strip().lower()
        if not code.isalpha() or len(code) != 2:
            raise ValueError(f"language must be a two-letter ISO 639-1 code: {value!r}")
        return code

    @field_validator("emotion")
    @classmethod
    def _emotion_is_known(cls, value: str | None) -> str | None:
        """Reject emotion tags the engines do not know about.

        Args:
            value: Emotion tag supplied for the role.

        Returns:
            The lowercased tag, or None when unset.

        Raises:
            ValueError: If the tag is not a supported emotion.
        """
        if value is None:
            return None
        tag = value.strip().lower()
        if tag not in EMOTION_TAGS:
            raise ValueError(f"emotion must be one of {sorted(EMOTION_TAGS)}: {value!r}")
        return tag


class MusicBed(BaseModel):
    """A local music track laid under the narration.

    Tracks are files inside `narration_music_dir`; nothing is ever downloaded.

    Attributes:
        track: File name of the track inside the music directory.
        volume_db: Track volume relative to the voice mix, in dB.
        fade_in_ms: Fade-in applied to the music.
        fade_out_ms: Fade-out applied to the music.
        duck_db: How much the music drops while the voice is speaking.
    """

    track: str = Field(
        ..., min_length=1, max_length=128, description="File name inside narration_music_dir"
    )
    volume_db: float = Field(default=-18.0, ge=-60.0, le=0.0, description="Music volume in dB")
    fade_in_ms: int = Field(default=800, ge=0, le=10_000, description="Music fade-in in ms")
    fade_out_ms: int = Field(default=1200, ge=0, le=10_000, description="Music fade-out in ms")
    duck_db: float = Field(
        default=-9.0, ge=-40.0, le=0.0, description="Extra attenuation applied under the voice"
    )


class NarrationSpec(BaseModel):
    """What to render: the cast, the delivery defaults and the music bed.

    Attributes:
        title: Program title, used in logs and the admin GUI.
        language: Default ISO 639-1 language for the whole piece.
        roles: The cast; segment role names must reference one of these.
        pause_ms: Silence inserted between consecutive turns.
        max_chars_per_request: Client-side chunking size (the gateway does not
            chunk long text, so this bounds each synthesis call).
        music: Optional music bed.
        speed: Default speaking rate for roles that do not override it.
        emotion: Default emotion for roles that do not override it.
        tts_backend: Engine that synthesizes this narration.
        voice_source: Where the reference voice comes from (non-OMNIVOICE only).
        voice_ref: Reference voice name from the catalog (non-OMNIVOICE only).
        ai_disclosure: Prepend an audible AI-generated disclosure.
    """

    title: str = Field(default="", max_length=200, description="Program title")
    language: str = Field(default="es", description="ISO 639-1 code")
    roles: list[SpeakerRole] = Field(
        ..., min_length=1, max_length=8, description="Cast, at least one role"
    )
    pause_ms: int = Field(
        default=400, ge=0, le=5000, description="Silence between consecutive turns, in ms"
    )
    max_chars_per_request: int = Field(
        default=1500, ge=100, le=10_000, description="Max characters per synthesis request"
    )
    music: MusicBed | None = Field(default=None, description="Optional local music bed")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Default speaking rate")
    emotion: str | None = Field(default=None, description="Default emotion tag")
    tts_backend: TTSBackend = Field(
        default=TTSBackend.OMNIVOICE, description="Engine that synthesizes this narration"
    )
    voice_source: VoiceSource = Field(
        default=VoiceSource.QWEN_DESIGN,
        description="Where the reference voice comes from (non-OMNIVOICE only)",
    )
    voice_ref: str = Field(
        default="", description="Reference voice name from the catalog (non-OMNIVOICE only)"
    )
    ai_disclosure: bool = Field(
        default=False, description="Prepend an audible AI-generated disclosure"
    )
    run_in_modal: bool = Field(
        default=False,
        description="For QWEN3_TTS backend, run synthesis in Modal instead of gateway local",
    )

    @field_validator("language")
    @classmethod
    def _language_is_iso639_1(cls, value: str) -> str:
        """Normalize the piece language to a two-letter lowercase code.

        Args:
            value: Language code supplied for the spec.

        Returns:
            The lowercased two-letter code.

        Raises:
            ValueError: If the code is not a two-letter ISO 639-1 code.
        """
        code = value.strip().lower()
        if not code.isalpha() or len(code) != 2:
            raise ValueError(f"language must be a two-letter ISO 639-1 code: {value!r}")
        return code

    @field_validator("emotion")
    @classmethod
    def _emotion_is_known(cls, value: str | None) -> str | None:
        """Validate the default emotion tag against the supported set.

        Args:
            value: Emotion tag supplied for the spec.

        Returns:
            The lowercased tag, or None when unset.

        Raises:
            ValueError: If the tag is not a supported emotion.
        """
        if value is None:
            return None
        tag = value.strip().lower()
        if tag not in EMOTION_TAGS:
            raise ValueError(f"emotion must be one of {sorted(EMOTION_TAGS)}: {value!r}")
        return tag

    @model_validator(mode="after")
    def _role_names_are_unique(self) -> NarrationSpec:
        """Ensure the cast has no duplicate role names.

        Segments address roles by name, so duplicates would make a segment
        ambiguous.

        Returns:
            The validated spec.

        Raises:
            ValueError: If two roles share a name.
        """
        names = [role.name for role in self.roles]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate role names: {duplicates}")
        return self

    @model_validator(mode="after")
    def _cast_matches_the_backend(self) -> NarrationSpec:
        """Reject a cast the selected engine could never synthesize.

        Each role voice must belong to ``tts_backend``'s vocabulary and every
        known voice must share an engine (Phase 9.2): a Modal catalog name
        read by the gateway, or a mixed cast, would otherwise fall back to
        another voice or fail at 08:00 during the run.

        Returns:
            The validated spec.

        Raises:
            ValueError: If the cast and the engine do not line up. The guard
                lives in :mod:`src.api.services.voice_engine`, imported lazily
                so this model keeps pulling no service at module load.
        """
        from src.api.services.voice_engine import (
            VoiceEngineError,
            assert_same_engine,
            assert_voice_engine,
        )

        try:
            assert_same_engine([role.voice_id for role in self.roles], field="roles")
            for role in self.roles:
                assert_voice_engine(role.voice_id, str(self.tts_backend), field="voice_id")
        except VoiceEngineError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def role(self, name: str) -> SpeakerRole:
        """Look up a role by name.

        Args:
            name: Role name referenced by a script segment.

        Returns:
            The matching role.

        Raises:
            KeyError: If no role has that name.
        """
        for candidate in self.roles:
            if candidate.name == name:
                return candidate
        raise KeyError(f"unknown role: {name!r}")


class NarrationSegment(BaseModel):
    """One spoken turn of the script.

    Attributes:
        role: Name of the role that speaks this segment.
        text: What is said. Kept free of markdown; the renderer normalizes it.
        tts_text: Optional pronunciation-adjusted text. When set, the renderer
            synthesizes this instead of ``text``: it carries the phonetic
            respelling of foreign terms and is what makes a re-render free.
            ``text`` stays the canonical, human-readable script.
        emotion: Optional per-segment emotion override.
        pause_after_ms: Optional pause after this segment, overriding the spec.
    """

    role: str = Field(..., min_length=1, max_length=64, description="Role name from the spec")
    text: str = Field(..., min_length=1, max_length=4000, description="Spoken text")
    tts_text: str | None = Field(
        default=None,
        max_length=8000,
        description="Pronunciation-adjusted text synthesized instead of 'text'",
    )
    emotion: str | None = Field(default=None, description="Emotion tag for this segment")
    pause_after_ms: int | None = Field(
        default=None, ge=0, le=5000, description="Pause after this segment, in ms"
    )

    @field_validator("emotion")
    @classmethod
    def _emotion_is_known(cls, value: str | None) -> str | None:
        """Validate a per-segment emotion tag against the supported set.

        Args:
            value: Emotion tag supplied for the segment.

        Returns:
            The lowercased tag, or None when unset.

        Raises:
            ValueError: If the tag is not a supported emotion.
        """
        if value is None:
            return None
        tag = value.strip().lower()
        if tag not in EMOTION_TAGS:
            raise ValueError(f"emotion must be one of {sorted(EMOTION_TAGS)}: {value!r}")
        return tag


class NarrationScript(BaseModel):
    """An ordered script: segments plus the metadata needed to render it.

    Attributes:
        title: Program title.
        language: ISO 639-1 language of the text.
        source: Where the script came from ('llm', 'manual' or 'api').
        segments: Ordered turns; must be non-empty.
    """

    title: str = Field(default="", max_length=200, description="Program title")
    language: str = Field(default="es", description="ISO 639-1 code")
    source: str = Field(default="llm", description="Origin of the script")
    segments: list[NarrationSegment] = Field(
        ..., min_length=1, max_length=200, description="Ordered spoken turns"
    )

    @field_validator("source")
    @classmethod
    def _source_is_known(cls, value: str) -> str:
        """Validate the script origin.

        Args:
            value: Source supplied for the script.

        Returns:
            The lowercased source.

        Raises:
            ValueError: If the source is not one of SCRIPT_SOURCES.
        """
        source = value.strip().lower()
        if source not in SCRIPT_SOURCES:
            raise ValueError(f"source must be one of {list(SCRIPT_SOURCES)}: {value!r}")
        return source

    @field_validator("language")
    @classmethod
    def _language_is_iso639_1(cls, value: str) -> str:
        """Normalize the script language to a two-letter lowercase code.

        Args:
            value: Language code supplied for the script.

        Returns:
            The lowercased two-letter code.

        Raises:
            ValueError: If the code is not a two-letter ISO 639-1 code.
        """
        code = value.strip().lower()
        if not code.isalpha() or len(code) != 2:
            raise ValueError(f"language must be a two-letter ISO 639-1 code: {value!r}")
        return code

    @model_validator(mode="after")
    def _segments_reference_known_roles(self, info: object) -> NarrationScript:
        """Ensure every segment names a role that exists in the paired spec.

        The spec travels in the validation context, so this check only runs when
        the caller validates a ``(NarrationScript, NarrationSpec)`` pair (as the
        renderer does) and is skipped for standalone scripts.

        Args:
            info: Pydantic validation context.

        Returns:
            The validated script.

        Raises:
            ValueError: If a segment references a role outside the spec cast.
        """
        context = getattr(info, "context", None) or {}
        spec = context.get("spec")
        if spec is None:
            return self
        known = {role.name for role in spec.roles}
        unknown = sorted({segment.role for segment in self.segments if segment.role not in known})
        if unknown:
            raise ValueError(f"segments reference unknown roles: {unknown}")
        return self

    def total_chars(self) -> int:
        """Return the number of characters to synthesize.

        Returns:
            Sum of the segment text lengths.
        """
        return sum(len(segment.text) for segment in self.segments)


class GatewayHealthOut(BaseModel):
    """Health of the TTS gateway that serves the narration cast.

    Attributes:
        reachable: False when the gateway could not be contacted at all.
        status: Gateway status ('ok', 'degraded', 'not_initialized').
        engine: Active engine name (e.g. 'routed(edgetts, pocket_tts)').
        device: Inference device reported by the engine.
        mode: Engine mode (e.g. 'ROUTED', 'OMNIVOICE', 'MOCK').
        stock_voices_count: Number of stock voices the engine offers.
        gpu_available: Whether a GPU is in use.
        error: Populated when the gateway answered with an error status.
    """

    reachable: bool = Field(default=True, description="False when the gateway is unreachable")
    status: str = Field(default="unknown", description="Gateway status")
    engine: str = Field(default="", description="Active engine name")
    device: str = Field(default="", description="Inference device")
    mode: str = Field(default="", description="Engine mode")
    stock_voices_count: int = Field(default=0, ge=0, description="Stock voices offered")
    gpu_available: bool = Field(default=False, description="Whether a GPU is in use")
    error: str | None = Field(default=None, description="Error detail when degraded or down")


class NarrationOut(BaseModel):
    """Result of rendering a narration script to audio.

    Attributes:
        audio_path: Final deliverable (M4A), ready for the delivery channels.
        wav_path: Canonical mono 24 kHz WAV master kept in the narration cache.
        duration_seconds: Measured duration of the master.
        cached: True when a previous render with identical inputs was reused.
        segments: Number of script segments rendered.
        char_count: Number of characters synthesized.
        voices: Voice ids actually used, in order of first appearance.
        used_conversations_api: Whether the multi-turn gateway endpoint was used.
        edge_fallbacks: Number of turns that fell back to a direct Edge call.
        conversation_blocks: Gateway requests the conversation was split into.
        music: Music track mixed in, if any.
        title: Program title of the rendered piece.
        tts_backend: Engine that synthesized the audio.
        voice_source: Source of the reference voice.
        ai_disclosure: Whether an audible AI disclosure was prepended.
    """

    audio_path: str = Field(..., description="Final M4A path for delivery")
    wav_path: str = Field(..., description="Canonical WAV master path")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Measured duration")
    cached: bool = Field(default=False, description="True when served from cache")
    segments: int = Field(default=0, ge=0, description="Script segments rendered")
    char_count: int = Field(default=0, ge=0, description="Characters synthesized")
    voices: list[str] = Field(default_factory=list, description="Voice ids used")
    used_conversations_api: bool = Field(
        default=False, description="True when /conversations was used"
    )
    edge_fallbacks: int = Field(default=0, ge=0, description="Turns served by direct Edge")
    conversation_blocks: int = Field(
        default=1, ge=1, description="Gateway requests the conversation was split into"
    )
    music: str | None = Field(default=None, description="Music track mixed in")
    title: str = Field(default="", description="Program title")
    tts_backend: str = Field(default="", description="Engine that synthesized the audio")
    voice_source: str = Field(default="", description="Source of the reference voice")
    ai_disclosure: bool = Field(
        default=False, description="Whether an audible AI disclosure was prepended"
    )
