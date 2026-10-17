"""Excepciones personalizadas de la API de OmniVoice."""

from __future__ import annotations


class OmniVoiceAPIError(Exception):
    """Excepción base para todos los errores de la API de OmniVoice."""

    pass


class VoiceNotFoundError(OmniVoiceAPIError):
    """Se lanza cuando no se encuentra una voz solicitada."""

    def __init__(self, voice_id: str, voice_type: str = "stock") -> None:
        self.voice_id = voice_id
        self.voice_type = voice_type
        self.valid_voice_ids: list[str] | None = None
        super().__init__(f"Voz no encontrada: {voice_id} ({voice_type})")


class UnsupportedLanguageError(OmniVoiceAPIError):
    """Se lanza cuando se solicita un idioma no soportado."""

    def __init__(self, language: str, supported_languages: list[str]) -> None:
        self.language = language
        self.supported_languages = supported_languages
        super().__init__(
            f"Idioma no soportado: {language}. Idiomas soportados: {', '.join(supported_languages)}"
        )


class UnsupportedEmotionError(OmniVoiceAPIError):
    """Se lanza cuando se solicita una emoción no soportada."""

    def __init__(self, emotion: str, supported_emotions: list[str]) -> None:
        self.emotion = emotion
        self.supported_emotions = supported_emotions
        super().__init__(
            f"Emoción no soportada: {emotion}. "
            f"Emociones soportadas: {', '.join(supported_emotions)}"
        )


class UnsupportedInstructError(OmniVoiceAPIError):
    """Se lanza cuando el instruct contiene tokens no soportados por el modelo."""

    def __init__(
        self, instruct: str, invalid_items: dict[str, str | None], valid_items: list[str]
    ) -> None:
        self.instruct = instruct
        self.invalid_items = invalid_items
        self.valid_items = valid_items
        lines = [f"Instruct inválido: '{instruct}'"]
        for item, suggestion in invalid_items.items():
            hint = f" (¿quisiste decir '{suggestion}'?)" if suggestion else ""
            lines.append(f"  '{item}' -> '{item}' (no soportado{hint})")
        lines.append(f"Tokens válidos: {', '.join(valid_items)}")
        super().__init__("\n".join(lines))


class EngineUnavailableError(OmniVoiceAPIError):
    """Se lanza cuando el motor de síntesis no está disponible."""

    def __init__(self, detail: str = "Motor de síntesis no disponible") -> None:
        self.detail = detail
        super().__init__(detail)


class InvalidReferenceAudioError(OmniVoiceAPIError):
    """Se lanza cuando el audio de referencia para clonación de voz es inválido."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class FeatureNotSupportedError(OmniVoiceAPIError):
    """Se lanza cuando se solicita una feature no soportada por el engine activo."""

    def __init__(self, feature: str, engine: str) -> None:
        self.feature = feature
        self.engine = engine
        super().__init__(
            f"Feature '{feature}' no soportada por el engine '{engine}'. "
            f"Cambia TTS_ENGINE o usa un engine que soporte esta feature."
        )
