import json
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    APP_NAME: str = "TTS API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"

    # --- TTS Engine Selection ---
    # omnivoice: GPU-based, full features (stock, clone, instruct, emotions)
    # pocket_tts: CPU-first, voice cloning, es/en/fr (no Catalan)
    # edgetts: cloud, no cloning, 400+ voices including Catalan (ca-ES)
    # piper: CPU-first local neural TTS, incl. Catalan (ca-ES)
    # chatterbox: GPU multilingual TTS, zero-shot cloning (no Catalan)
    # mock: test tones (for development/testing)
    # routed: multi-engine, dispatches by language (requires TTS_ENGINES config)
    TTS_ENGINE: Literal[
        "omnivoice", "pocket_tts", "edgetts", "piper", "chatterbox", "mock", "routed"
    ] = "omnivoice"

    # Multi-engine routing (when TTS_ENGINE=routed)
    # JSON dict mapping language codes to engine names.
    # Use "_default" for fallback when language has no explicit mapping.
    # Ejemplo de mapeo: es a pocket_tts, en a pocket_tts, ca a edgetts, y _default a pocket_tts.
    TTS_ENGINES: str = ""

    # Pocket TTS config (when TTS_ENGINE=pocket_tts)
    # Código de idioma para load_model() (p. ej. "english") o ruta/URL de un
    # fichero de configuración YAML ("hf://<repo>/config.yaml"). Un repo id de
    # Hugging Face sin .yaml no sirve y se ignora con warning.
    POCKET_TTS_MODEL: str = "english"

    # EdgeTTS config (when TTS_ENGINE=edgetts)
    EDGETTS_VOICE_PREFIX: str = "es-MX"

    # --- Piper engine (when TTS_ENGINE=piper) ---
    # Directorio con los modelos ONNX (<voice>.onnx + <voice>.onnx.json).
    PIPER_VOICES_DIR: Path = Path("storage/piper_voices")
    # Voz por defecto (nombre de modelo Piper, p.ej. "ca_ES-upc_ona-medium").
    PIPER_VOICE: str = "ca_ES-upc_ona-medium"
    # Acelerar con ONNX Runtime CUDA (requiere onnxruntime-gpu instalado).
    PIPER_USE_CUDA: bool = False
    # Descargar la voz desde Hugging Face si falta en PIPER_VOICES_DIR.
    PIPER_AUTO_DOWNLOAD: bool = True

    # --- Chatterbox engine (when TTS_ENGINE=chatterbox) ---
    # Variante del checkpoint multilingüe: "v3" (por defecto) o "v2".
    CHATTERBOX_T3_MODEL: Literal["v2", "v3"] = "v3"
    # Dispositivo torch ("cuda" o "cpu"); cae a cpu si CUDA no está disponible.
    CHATTERBOX_DEVICE: str = "cuda"
    # Estilos de generación por defecto (0.25-2.0 exaggeration, 0.2-1.0 cfg).
    CHATTERBOX_EXAGGERATION: float = 0.5
    CHATTERBOX_CFG_WEIGHT: float = 0.5

    # Auto-transcription of reference audio on voice clone (requires faster-whisper)
    AUTO_TRANSCRIBE: bool = False
    # Whisper model size: tiny, base, small, medium, large-v3
    TRANSCRIBE_MODEL: str = "base"

    # --- OmniVoice Engine (legacy, kept for backward compat) ---
    TTS_MODEL_ID: str = "ModelsLab/omnivoice-singing"
    TTS_DTYPE: Literal["float16", "float32", "int8"] = "float16"
    TTS_WARMUP_ON_START: bool = False
    TTS_DEVICE: str = "cuda:0"

    # --- Mock / Real engine toggle ---
    # Si True, usa generación de tonos mock (útil para tests sin GPU/modelo).
    # Si False (por defecto), usa el motor OmniVoice real.
    TTS_USE_MOCK: bool = False
    # Si True, cuando el motor real falle al arrancar, la API cae
    # automáticamente al modo mock en lugar de crashear. El endpoint
    # /api/v1/health devolverá status="degraded" y el campo
    # real_engine_error con la causa raíz.
    TTS_FALLBACK_TO_MOCK: bool = True

    # --- Database ---
    DATABASE_URL: str = "sqlite:///storage/omnivoice.db"

    # --- Storage ---
    STORAGE_BASE_PATH: Path = Path("storage")
    VOICES_DIR: Path = Path("storage/voices")
    OUTPUTS_DIR: Path = Path("storage/outputs")
    CACHE_DIR: Path = Path("storage/cache")
    OUTPUT_TTL_SECONDS: int = 3600

    # --- Security ---
    API_KEY: str = ""
    CORS_ORIGINS: str = "*"
    MAX_UPLOAD_SIZE_MB: int = 10
    MAX_REFERENCE_DURATION_SEC: float = 30.0

    # --- Observability ---
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    @property
    def cors_origins_list(self) -> list[str]:
        """Lista de orígenes CORS como lista de strings."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def python_bin(self) -> Path:
        """Ruta al python del venv externo de OmniVoice."""
        from tts_api.core.engine_paths import default_venv_dir, python_bin_from_venv

        return python_bin_from_venv(default_venv_dir())

    @property
    def model_path(self) -> Path:
        """Ruta al modelo OmniVoice descargado."""
        from tts_api.core.engine_paths import default_install_dir

        return default_install_dir() / "models"

    @property
    def omnilang_list(self) -> list[str]:
        """Idiomas soportados por OmniVoice.

        Cuando el engine es ``routed``, se amplía con los idiomas declarados
        en ``TTS_ENGINES`` (p.ej. ``ca``) para que el validado de idioma del
        servicio no bloquee el routing.
        """
        langs = ["es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"]
        if self.TTS_ENGINE == "routed" and self.TTS_ENGINES.strip():
            try:
                routing = json.loads(self.TTS_ENGINES)
            except json.JSONDecodeError:
                routing = {}
            if isinstance(routing, dict):
                langs.extend(code for code in routing if code and code != "_default")
        return sorted(set(langs))


def get_settings() -> Settings:
    """Obtener una nueva instancia de configuración que lee las variables de entorno actuales."""
    return Settings()
