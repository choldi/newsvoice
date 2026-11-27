"""Service for voice operations (cloning + designed)."""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any, cast

from loguru import logger
from tts_contract import canonical_engine_id

from tts_api.core.audio import AudioValidator
from tts_api.core.embedding_cache import EmbeddingCache, get_embedding_cache
from tts_api.core.engine_registry import EngineRegistry, engine_supports_capability
from tts_api.core.exceptions import (
    FeatureNotSupportedError,
    InvalidReferenceAudioError,
    UnsupportedInstructError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.core.omnivoice_engine import _validate_instruct
from tts_api.repositories.voice_repository import VoiceRepository
from tts_api.settings import get_settings

MIN_DESIGN_INSTRUCT_LENGTH = 10


class VoiceService:
    """Service for voice operations (cloning + designed)."""

    def __init__(
        self,
        repository: VoiceRepository | None = None,
        audio_validator: AudioValidator | None = None,
        embedding_cache: EmbeddingCache | None = None,
    ) -> None:
        self._settings = get_settings()
        self._repository = repository
        self._audio_validator = audio_validator
        self._embedding_cache = embedding_cache
        self._initialized = repository is not None

    @property
    def _active_engine(self) -> str:
        """Name of the active engine (handles routed)."""
        engine = self._settings.effective_tts_engine
        if engine == "routed":
            return "pocket_tts"
        return engine

    def _supports(self, engine: str, capability: str) -> bool:
        """Check a declared engine capability from the registry."""
        registry = getattr(self._settings, "engine_registry", None)
        if not isinstance(registry, EngineRegistry):
            registry = EngineRegistry.load()
        return engine_supports_capability(registry, engine, capability, self._settings)

    async def initialize(self) -> None:
        """Initialize the service and its dependencies."""
        if self._repository is None:
            self._repository = VoiceRepository()
            await self._repository.initialize()

        if self._audio_validator is None:
            self._audio_validator = AudioValidator(engine_name=self._active_engine)

        if self._embedding_cache is None:
            self._embedding_cache = get_embedding_cache()

        self._initialized = True

    async def clone_voice(
        self,
        name: str,
        language: str,
        reference_audio_path: Path | str,
        ref_text: str | None = None,
        engine: str | list[str] | None = None,
    ) -> str:
        """Clone a voice from reference audio.

        Args:
            name: Unique name for the cloned voice
            language: Language code (ISO 639-1)
            reference_audio_path: Path to the reference audio file
            ref_text: Transcription of the reference audio (optional, improves cloning)
            engine: Engine(s) to associate. Accepts "pocket_tts",
                    "pocket_tts,omnivoice", or list. None = active engine.

        Returns:
            str: The UUID of the cloned voice

        Raises:
            ValueError: If voice name already exists
            UnsupportedLanguageError: If language is not supported
            InvalidReferenceAudioError: If reference audio is invalid
            FeatureNotSupportedError: If an engine doesn't support cloning
        """
        # Parse engine(s)
        if engine is None:
            engine_str = self._active_engine
        elif isinstance(engine, list):
            engine_str = ",".join(engine)
        else:
            engine_str = engine.strip()

        engine_list = [canonical_engine_id(e.strip()) for e in engine_str.split(",") if e.strip()]

        # Validate each engine supports cloning
        for e in engine_list:
            if not self._supports(e, "voice_cloning"):
                raise FeatureNotSupportedError("voice cloning", e)

        # Validate language
        if language not in self._settings.omnilang_list:
            raise UnsupportedLanguageError(language, self._settings.omnilang_list)

        # Pick sample rate: 24000 if pocket_tts included (max quality), else 22050
        validator_engine = "pocket_tts" if "pocket_tts" in engine_list else engine_list[0]

        audio_validator = AudioValidator(engine_name=validator_engine)
        validated_path, duration_sec = await audio_validator.validate_and_prepare(
            reference_audio_path, language
        )

        # Check max duration
        if duration_sec > self._settings.MAX_REFERENCE_DURATION_SEC:
            raise InvalidReferenceAudioError(
                f"Reference audio too long: {duration_sec:.1f}s > "
                f"{self._settings.MAX_REFERENCE_DURATION_SEC}s"
            )

        # Copy to permanent storage
        voices_dir = self._settings.VOICES_DIR
        voices_dir.mkdir(parents=True, exist_ok=True)

        voice_id = str(uuid.uuid4())
        dest_path = voices_dir / voice_id / "reference.wav"
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(validated_path, dest_path)

        metadata = {"original_path": str(reference_audio_path)}
        if ref_text:
            metadata["ref_text"] = ref_text

        # Store in repository with engine tag (comma-separated)
        engine_tag = ",".join(engine_list)
        repository = cast(VoiceRepository, self._repository)
        voice_id = await repository.create(
            name=name,
            language=language,
            reference_path=str(dest_path),
            duration_sec=duration_sec,
            metadata=metadata,
            engine=engine_tag,
        )

        # Pre-compute and cache embedding
        try:
            cache = cast(EmbeddingCache, self._embedding_cache)
            cache.invalidate(dest_path)
        except Exception as e:
            logger.warning(f"Failed to precompute embedding for {voice_id}: {e}")

        logger.info(f"Voice cloned successfully: {name} ({voice_id}, engines={engine_tag})")
        return voice_id

    async def get_voice(self, voice_id: str) -> dict[str, Any]:
        """Get a cloned voice by ID. Validates engine compatibility."""
        repository = cast(VoiceRepository, self._repository)
        voice = await repository.get_by_id(voice_id)
        self._validate_engine(voice)
        return voice

    async def list_voices(
        self,
        language: str | None = None,
        limit: int = 100,
        offset: int = 0,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """List cloned voices with optional filtering."""
        repository = cast(VoiceRepository, self._repository)
        return await repository.list(
            language=language,
            limit=limit,
            offset=offset,
            engine=engine,
        )

    async def delete_voice(self, voice_id: str) -> bool:
        """Delete a cloned voice."""
        ref_path = None
        repository = cast(VoiceRepository, self._repository)
        try:
            voice = await repository.get_by_id(voice_id)
            ref_path = Path(voice["reference_path"])
            if ref_path.exists():
                shutil.rmtree(ref_path.parent, ignore_errors=True)
        except VoiceNotFoundError:
            pass

        deleted = await repository.delete(voice_id)

        if deleted and ref_path is not None:
            with contextlib.suppress(Exception):
                cache = cast(EmbeddingCache, self._embedding_cache)
                cache.invalidate(ref_path)

        return deleted

    async def voice_exists(self, voice_id: str) -> bool:
        """Check if a voice exists."""
        repository = cast(VoiceRepository, self._repository)
        return await repository.voice_exists(voice_id)

    def _validate_engine(self, voice: dict[str, Any]) -> None:
        """Validate that the voice's engine(s) include the active engine.

        The voice's engine column stores comma-separated engines
        (e.g. "pocket_tts,omnivoice").

        Raises:
            FeatureNotSupportedError: If active engine not in voice's engine list.
        """
        voice_engines_raw = voice.get("engine", "omnivoice")
        voice_engines = {e.strip() for e in voice_engines_raw.split(",") if e.strip()}
        active = self._active_engine

        # For routed engines, accept voices tagged with any clone-capable engine
        is_routed = self._settings.effective_tts_engine == "routed"
        if is_routed and any(self._supports(e, "voice_cloning") for e in voice_engines):
            return

        # Direct match: active engine is in the voice's engine list
        if active in voice_engines:
            return

        raise FeatureNotSupportedError(
            f"cloned voice (engines={voice_engines_raw})",
            active,
        )

    # --- Designed voices (instruct-based presets) ---

    async def create_designed_voice(
        self,
        name: str,
        instruct: str,
        language: str,
        engine: str | None = None,
    ) -> str:
        """Create a designed voice preset pinned to a design-capable engine."""
        if language not in self._settings.omnilang_list:
            raise UnsupportedLanguageError(language, self._settings.omnilang_list)

        design_engine = canonical_engine_id((engine or self._settings.effective_tts_engine).strip())
        if design_engine == "routed" or not self._supports(design_engine, "instruct_voice_design"):
            raise FeatureNotSupportedError("voice design", design_engine)

        if design_engine in ("omnivoice", "omnivoice_remote"):
            _validate_instruct(instruct)
        elif len(instruct.strip()) < MIN_DESIGN_INSTRUCT_LENGTH:
            raise UnsupportedInstructError(
                instruct,
                {"instruct": "debe contener al menos 10 caracteres"},
                [],
            )

        repository = cast(VoiceRepository, self._repository)
        voice_id = await repository.create_designed_voice(
            name=name,
            instruct=instruct,
            language=language,
            metadata={"engine": design_engine},
        )
        logger.info(f"Designed voice created: {name} ({voice_id}) on {design_engine}")
        return voice_id

    async def get_designed_voice(self, voice_id: str) -> dict[str, Any]:
        """Get a designed voice by ID."""
        repository = cast(VoiceRepository, self._repository)
        return await repository.get_designed_voice(voice_id)

    async def list_designed_voices(
        self,
        language: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List designed voices with optional filtering."""
        repository = cast(VoiceRepository, self._repository)
        return await repository.list_designed_voices(
            language=language,
            limit=limit,
            offset=offset,
        )

    async def delete_designed_voice(self, voice_id: str) -> bool:
        """Delete a designed voice."""
        repository = cast(VoiceRepository, self._repository)
        return await repository.delete_designed_voice(voice_id)
