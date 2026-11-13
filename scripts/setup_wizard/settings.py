"""Curated configuration model: which keys the wizard asks for, and why.

Every screen in the assistant is a projection of :data:`SECTIONS`, so the
curses renderer and the non interactive fallback share one source of truth.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

__all__ = [
    "EngineInfo",
    "ENGINE_INFO",
    "GPU_ENGINES",
    "MODEL_OPTIONS",
    "ROUTED_PRESETS",
    "RoutedPreset",
    "Field",
    "Profile",
    "PROFILES",
    "SECTIONS",
    "configured_engines",
    "config_requires_gpu",
    "engine_info",
    "fields_for",
    "hash_password",
    "join_multi",
    "parse_engine_map",
    "profiles",
    "render_engine_map",
    "sections_for",
    "split_multi",
    "valid_language_code",
]

# -- password helpers -------------------------------------------------------


def hash_password(password: str) -> str | None:
    """Return a bcrypt hash for ``password``.

    Args:
        password: Plain text password.

    Returns:
        The ``$2b$...`` hash, or ``None`` when ``bcrypt`` is unavailable.
    """
    try:
        import bcrypt
    except ImportError:  # pragma: no cover - exercised on bare interpreters
        return None
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


# -- data model -------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One configuration value the wizard collects.

    Attributes:
        file: Target env file (``deploy``, ``news`` or ``gateway``).
        key: Variable name.
        label: Prompt shown in the UI.
        help: One line explanation.
        kind: ``text``, ``secret``, ``choice``, ``bool``, ``multi`` or
            ``password``.
        default: Template fallback value.
        options: Allowed values for ``choice``.
        generate: ``hex32`` when a random secret can be minted for it.
        required: Surfaced by validation as mandatory.
        advanced: Hidden until the advanced toggle is enabled.
    """

    file: str
    key: str
    label: str
    help: str
    kind: str = "text"
    default: str = ""
    options: tuple[str, ...] = ()
    generate: str = ""
    required: bool = False
    advanced: bool = False

    @property
    def detail(self) -> str:
        """Return the long help text, falling back to the short summary."""
        return _FIELD_DETAILS.get((self.file, self.key), self.help)

    @property
    def source(self) -> str:
        """Return ``interno`` for stack values, ``externo`` for provider APIs."""
        return "externo" if (self.file, self.key) in _EXTERNAL_KEYS else "interno"


@dataclass(frozen=True)
class Section:
    """A group of fields rendered under one heading.

    Attributes:
        file: Target env file.
        title: Heading text.
        fields: The fields belonging to the group, in prompt order.
    """

    file: str
    title: str
    fields: tuple[Field, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Profile:
    """A starting point that pre-fills values for one deployment shape.

    Attributes:
        key: Stable id used by the CLI.
        label: Menu entry text.
        description: Long explanation shown next to the selection.
        defaults: ``(file, key) -> value`` overrides applied to the fields.
    """

    key: str
    label: str
    description: str
    defaults: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineInfo:
    """Runtime capabilities shown by the TTS setup screen.

    Attributes:
        key: Engine id used by ``TTS_ENGINE`` and ``TTS_ENGINES``.
        label: Human readable name.
        device: ``GPU``, ``CPU``, ``Cloud`` or ``Proxy``.
        cloning: Whether the engine can clone voices.
        languages: ISO 639-1 languages known to the engine.
        description: Short explanation of the engine and its limits.
        downloads: Local checkpoints the wizard can record for prefetch.
    """

    key: str
    label: str
    device: str
    cloning: bool
    languages: tuple[str, ...]
    description: str
    downloads: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutedPreset:
    """A ready-made language routing configuration.

    Attributes:
        key: Stable preset id.
        label: Menu entry text.
        description: What the preset configures.
        engines: ``TTS_ENGINES`` JSON.
        extra_engines: ``TTS_EXTRA_ENGINES`` value.
        device: ``TTS_DEVICE`` value.
        model_downloads: Optional ``TTS_MODEL_DOWNLOADS`` value.
    """

    key: str
    label: str
    description: str
    engines: str
    extra_engines: str
    device: str
    model_downloads: str = ""


# -- fields -----------------------------------------------------------------

DEPLOY = "deploy"
NEWS = "news"
GATEWAY = "gateway"

_TTS_ENGINES = (
    '{"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}'
)

ENGINE_INFO: dict[str, EngineInfo] = {
    "pocket_tts": EngineInfo(
        "pocket_tts",
        "Pocket TTS",
        "CPU",
        True,
        ("en", "es", "fr", "de", "it", "pt"),
        "Clonado local en CPU; no cubre catalán.",
        ("pocket_tts:es", "pocket_tts:en", "pocket_tts:fr"),
    ),
    "piper": EngineInfo(
        "piper",
        "Piper",
        "CPU",
        False,
        ("ca", "es", "en", "fr"),
        "Voces ONNX ligeras en CPU; incluye catalán, sin clonado.",
        (
            "piper:ca_ES-upc_ona-medium",
            "piper:es_ES-davefx-medium",
            "piper:en_US-lessac-medium",
            "piper:fr_FR-siwis-medium",
        ),
    ),
    "kokoro": EngineInfo(
        "kokoro",
        "Kokoro",
        "CPU",
        False,
        ("en", "es", "fr", "hi", "it", "pt"),
        "Voces stock 82M en CPU; sin catalán ni clonado.",
        ("kokoro:hexgrad/Kokoro-82M",),
    ),
    "edgetts": EngineInfo(
        "edgetts",
        "Edge TTS",
        "Cloud",
        False,
        ("es", "en", "fr", "de", "it", "pt", "ca"),
        "400+ voces cloud, incluido catalán; sin clonado.",
    ),
    "chatterbox": EngineInfo(
        "chatterbox",
        "Chatterbox",
        "GPU",
        True,
        (
            "ar",
            "da",
            "de",
            "el",
            "en",
            "es",
            "fi",
            "fr",
            "he",
            "hi",
            "it",
            "ja",
            "ko",
            "ms",
            "nl",
            "no",
            "pl",
            "pt",
            "ru",
            "sv",
            "sw",
            "tr",
            "zh",
        ),
        "Clonado zero-shot y 23 idiomas; no cubre catalán.",
        ("chatterbox:ResembleAI/chatterbox",),
    ),
    "qwen3_tts": EngineInfo(
        "qwen3_tts",
        "Qwen3-TTS",
        "GPU",
        True,
        ("en", "es", "fr", "ca", "pt", "de", "it", "ja", "ko", "ru", "zh"),
        "Sidecar GPU con voces stock, diseño y clonado; cubre catalán.",
        (
            "qwen3_tts:Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            "qwen3_tts:Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            "qwen3_tts:Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        ),
    ),
    "omnivoice": EngineInfo(
        "omnivoice",
        "OmniVoice",
        "GPU",
        True,
        ("es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"),
        "Engine GPU completo: stock, clonado, instruct y emociones.",
        ("omnivoice:ModelsLab/omnivoice-singing",),
    ),
    "omnivoice_remote": EngineInfo(
        "omnivoice_remote",
        "OmniVoice remoto",
        "Proxy",
        True,
        ("es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"),
        "Proxy HTTP al sidecar OmniVoice; el sidecar necesita GPU.",
    ),
    "fish_tts": EngineInfo(
        "fish_tts",
        "Fish Audio",
        "Cloud",
        True,
        ("es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"),
        "Cloud con referencias stock y clones inline; requiere FISH_API_KEY.",
    ),
    "openai_tts": EngineInfo(
        "openai_tts",
        "OpenAI-compatible TTS",
        "Cloud",
        False,
        ("es", "en"),
        "Endpoint cloud /audio/speech; requiere OPENAI_TTS_BASE_URL.",
    ),
    "mock": EngineInfo(
        "mock",
        "Mock",
        "CPU",
        False,
        ("*",),
        "Tonos de prueba para desarrollo; no sintetiza voces reales.",
    ),
}

GPU_ENGINES = frozenset(key for key, info in ENGINE_INFO.items() if info.device in ("GPU", "Proxy"))
CLOUD_ENGINES = frozenset(key for key, info in ENGINE_INFO.items() if info.device == "Cloud")
ENGINE_OPTIONS = ("routed", *ENGINE_INFO)
EXTRA_ENGINE_OPTIONS = tuple(ENGINE_INFO)
MODEL_OPTIONS = tuple(download for info in ENGINE_INFO.values() for download in info.downloads)

_SAFE_TTS_ENGINES = (
    '{"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"piper","_default":"pocket_tts"}'
)
_SAFE_MODELS = ",".join(
    (
        "pocket_tts:es",
        "pocket_tts:en",
        "pocket_tts:fr",
        "piper:ca_ES-upc_ona-medium",
        "kokoro:hexgrad/Kokoro-82M",
    )
)

ROUTED_PRESETS: tuple[RoutedPreset, ...] = (
    RoutedPreset(
        "safe_cpu",
        "CPU seguro · Pocket + Piper catalán",
        "Todo local: es/en/fr con pocket_tts, ca con piper, extras kokoro/piper.",
        _SAFE_TTS_ENGINES,
        "kokoro,piper",
        "cpu",
        _SAFE_MODELS,
    ),
    RoutedPreset(
        "cpu_edge",
        "CPU + Edge catalán · Pocket + Edge",
        "es/en/fr con pocket_tts y ca con Edge TTS; extras kokoro.",
        '{"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts",'
        '"ca":"edgetts","_default":"pocket_tts"}',
        "kokoro",
        "cpu",
        "pocket_tts:es,pocket_tts:en,pocket_tts:fr,kokoro:hexgrad/Kokoro-82M",
    ),
    RoutedPreset(
        "gpu_omni",
        "GPU OmniVoice · Edge catalán",
        "es/en/fr con omnivoice, ca con edgetts; extras qwen3/omnivoice_remote.",
        '{"es":"omnivoice","en":"omnivoice","fr":"omnivoice",'
        '"ca":"edgetts","_default":"omnivoice"}',
        "qwen3_tts,omnivoice_remote",
        "cuda:0",
        "omnivoice:ModelsLab/omnivoice-singing,qwen3_tts:Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    ),
    RoutedPreset(
        "gpu_mixed",
        "GPU mixto · OmniVoice + Qwen3",
        "es/fr con omnivoice, en con qwen3_tts, ca con edgetts; extras kokoro.",
        '{"es":"omnivoice","en":"qwen3_tts","fr":"omnivoice",'
        '"ca":"edgetts","_default":"omnivoice"}',
        "kokoro,omnivoice_remote",
        "cuda:0",
        "omnivoice:ModelsLab/omnivoice-singing,qwen3_tts:Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    ),
)


def split_multi(value: str) -> list[str]:
    """Split a comma-separated multi-select value into unique tokens."""
    return list(dict.fromkeys(token.strip() for token in value.split(",") if token.strip()))


def join_multi(values: list[str]) -> str:
    """Join selected values in their original order, without duplicates."""
    return ",".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def engine_info(key: str) -> EngineInfo | None:
    """Return the catalog entry for ``key``, or ``None`` when unknown."""
    return ENGINE_INFO.get(key)


def parse_engine_map(value: str) -> dict[str, str] | None:
    """Parse ``TTS_ENGINES`` and return a string map, or ``None`` if invalid."""
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(engine) for key, engine in data.items()}


def render_engine_map(routing: dict[str, str]) -> str:
    """Render a routing map as compact JSON."""
    return json.dumps(routing, ensure_ascii=False, separators=(",", ":"))


def configured_engines(engine: str, engine_map: str, extra: str) -> list[str]:
    """Return every engine involved in the current selection."""
    names: list[str] = []
    if engine and engine != "routed":
        names.append(engine)
    if engine == "routed":
        routing = parse_engine_map(engine_map)
        if routing:
            names.extend(routing.values())
        names.extend(split_multi(extra))
    return list(dict.fromkeys(names))


def config_requires_gpu(engine: str, engine_map: str, extra: str) -> bool:
    """Report whether the selection instantiates an engine that needs GPU."""
    return any(name in GPU_ENGINES for name in configured_engines(engine, engine_map, extra))


def valid_language_code(value: str) -> bool:
    """Return True for ISO 639-1/639-2 style language keys."""
    return bool(re.fullmatch(r"[a-z]{2,3}", value))


_FIELD_DETAILS: dict[tuple[str, str], str] = {
    (DEPLOY, "API_PORT"): (
        "Puerto público del panel y la API en el host. Compose siempre publica el "
        "puerto 8000 del contenedor; si lo cambias, cambia también news/API_PORT."
    ),
    (DEPLOY, "GATEWAY_PORT"): (
        "Puerto público del TTS gateway en el host. Dentro de la red Docker el "
        "gateway sigue escuchando en gateway:8000."
    ),
    (DEPLOY, "GATEWAY_API_KEY"): (
        "Clave compartida X-API-Key. Se copia automáticamente a news/TTS_API_KEY "
        "y gateway/API_KEY; si queda vacía, el gateway queda abierto en la red."
    ),
    (DEPLOY, "COMPOSE_PROFILES"): (
        "Servicios que levanta `make up`. Shape A (un host) usa news,gateway; "
        "Shape B separa la app y el host GPU."
    ),
    (DEPLOY, "QWEN3_TTS_REMOTE_URL"): (
        "Activa el sidecar qwen3. En Docker usa http://gateway-qwen3:8000/api/v1; "
        "requiere GPU=1 y QWEN3=1 al desplegar."
    ),
    (NEWS, "ADMIN_USERNAME"): (
        "Usuario del panel admin. Si lo defines, define también "
        "ADMIN_PASSWORD_HASH o ADMIN_API_TOKEN."
    ),
    (NEWS, "ADMIN_PASSWORD_HASH"): (
        "Hash bcrypt de la contraseña, nunca la contraseña en claro. Enter abre "
        "el generador y la contraseña no se guarda."
    ),
    (NEWS, "ADMIN_API_TOKEN"): (
        "Token legacy para REST y GUI. Sirve como alternativa a usuario+hash; "
        "genera uno largo y no lo reutilices en otros servicios."
    ),
    (NEWS, "LLM_API_BASE"): (
        "Endpoint OpenAI-compatible del LLM (llama.cpp/LiteLLM). En Docker usa "
        "host.docker.internal, no localhost: dentro del contenedor localhost es "
        "el propio contenedor."
    ),
    (NEWS, "LLM_MODEL"): (
        "Nombre exacto del modelo que sirve LLM_API_BASE. Debe coincidir con el "
        "id publicado por /v1/models."
    ),
    (NEWS, "TELEGRAM_CHAT_ID"): (
        "Chat o canal de Telegram donde se entregan los boletines. Sin bot token no se envía nada."
    ),
    (NEWS, "TELEGRAM_BOT_TOKEN"): (
        "Token de @BotFather. No se puede generar automáticamente; vacío "
        "desactiva la entrega por Telegram."
    ),
    (NEWS, "SMTP_HOST"): (
        "Servidor SMTP para correo y códigos de acceso. Gmail usa smtp.gmail.com con STARTTLS."
    ),
    (NEWS, "SMTP_PORT"): ("Puerto SMTP. Gmail usa 587 con STARTTLS; 465 solo si usas SSL directo."),
    (NEWS, "SMTP_USER"): (
        "Usuario SMTP. Vacío desactiva el correo y los códigos de acceso por email."
    ),
    (NEWS, "SMTP_PASSWORD"): (
        "Contraseña de aplicación del correo, no la contraseña normal de la "
        "cuenta. Gmail la genera en myaccount.google.com/apppasswords."
    ),
    (NEWS, "TTS_API_BASE_URL"): (
        "URL del gateway TTS. En Docker debe ser http://gateway:8000/api/v1; no "
        "uses el puerto público 8001 desde otro contenedor."
    ),
    (NEWS, "TTS_API_KEY"): (
        "Copia de deploy/GATEWAY_API_KEY. Se sincroniza sola; si no coincide, el "
        "gateway responde 401."
    ),
    (NEWS, "TTS_DEFAULT_BACKEND"): (
        "Motor que heredan las colecciones sin tts_backend. edge no usa gateway; "
        "el resto necesita TTS_API_BASE_URL."
    ),
    (NEWS, "TTS_LANGUAGE"): (
        "Idioma ISO 639-1 por defecto. También instruye al LLM para escribir el "
        "digest en ese idioma."
    ),
    (NEWS, "TTS_VOICE"): (
        "Voz Edge neural por defecto (ShortName). Cada colección puede sobrescribirla."
    ),
    (NEWS, "DOC_TTS_LINK_SECRET"): (
        "Clave HMAC de los enlaces de audio firmados. Sin ella, el subido de "
        "documentos responde 503."
    ),
    (NEWS, "API_PORT"): (
        "Puerto de la API news. Debe coincidir con deploy/API_PORT para que el "
        "panel y los checks usen el mismo host."
    ),
    (GATEWAY, "TTS_ENGINE"): (
        "Motor principal. routed reparte por idioma con TTS_ENGINES; un valor "
        "fijo fuerza ese engine para todas las peticiones."
    ),
    (GATEWAY, "TTS_ENGINES"): (
        "JSON idioma→engine usado cuando TTS_ENGINE=routed. _default es el "
        "fallback para idiomas no listados."
    ),
    (GATEWAY, "TTS_EXTRA_ENGINES"): (
        "Engines extra disponibles para pins explícitos. Solo se cargan con TTS_ENGINE=routed. "
        "Puedes marcar varios; los engines GPU obligan a TTS_DEVICE=cuda:0."
    ),
    (GATEWAY, "TTS_MODEL_DOWNLOADS"): (
        "Lista de checkpoints locales que el instalador debe precargar (pocket_tts, piper, "
        "kokoro, qwen3_tts, omnivoice o chatterbox). Los engines cloud no descargan pesos."
    ),
    (GATEWAY, "TTS_DEVICE"): (
        "Dispositivo de inferencia. cuda:0 exige el overlay GPU; cpu funciona sin tarjeta gráfica."
    ),
    (GATEWAY, "OMNIVOICE_REMOTE_URL"): (
        "URL interna del sidecar OmniVoice. Vacío mantiene el proxy arrancado pero inactivo."
    ),
    (GATEWAY, "QWEN3_TTS_REMOTE_URL"): (
        "Solo desarrollo local; en Docker el valor se toma de deploy/QWEN3_TTS_REMOTE_URL."
    ),
    (GATEWAY, "QWEN3_TTS_REMOTE_TIMEOUT"): (
        "Segundos de espera del proxy qwen3. Súbelo si la primera carga del modelo tarda."
    ),
    (GATEWAY, "FISH_API_KEY"): (
        "API key de Fish Audio. Vacío desactiva Fish; actívalo con "
        "TTS_EXTRA_ENGINES=fish_tts o un pin engine=fish_tts."
    ),
    (GATEWAY, "FISH_MODEL"): "Modelo de Fish Audio usado para sintetizar voces Fish.",
    (GATEWAY, "FISH_API_BASE_URL"): (
        "Endpoint de la API de Fish Audio; normalmente https://api.fish.audio."
    ),
    (GATEWAY, "OPENAI_TTS_BASE_URL"): (
        "Base OpenAI-compatible para TTS; incluye /v1. Vacío desactiva este proveedor."
    ),
    (GATEWAY, "OPENAI_TTS_API_KEY"): ("Bearer opcional del endpoint OpenAI-compatible de TTS."),
    (GATEWAY, "OPENAI_TTS_MODEL"): "Modelo enviado a /audio/speech; por defecto tts-1.",
    (GATEWAY, "API_KEY"): (
        "Clave que protege el gateway. Debe ser igual a deploy/GATEWAY_API_KEY; se sincroniza sola."
    ),
    (GATEWAY, "HF_TOKEN"): (
        "Token de HuggingFace para descargar pesos gated de pocket_tts. Opcional; "
        "sin él el clonado de voces falla."
    ),
}

_EXTERNAL_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        (NEWS, "LLM_API_BASE"),
        (NEWS, "LLM_MODEL"),
        (NEWS, "TELEGRAM_CHAT_ID"),
        (NEWS, "TELEGRAM_BOT_TOKEN"),
        (NEWS, "SMTP_HOST"),
        (NEWS, "SMTP_PORT"),
        (NEWS, "SMTP_USER"),
        (NEWS, "SMTP_PASSWORD"),
        (GATEWAY, "FISH_API_KEY"),
        (GATEWAY, "FISH_MODEL"),
        (GATEWAY, "FISH_API_BASE_URL"),
        (GATEWAY, "OPENAI_TTS_BASE_URL"),
        (GATEWAY, "OPENAI_TTS_API_KEY"),
        (GATEWAY, "OPENAI_TTS_MODEL"),
        (GATEWAY, "HF_TOKEN"),
    }
)

_SECTIONS: list[Section] = [
    Section(
        file=DEPLOY,
        title="Puertos publicados",
        fields=(
            Field(
                file=DEPLOY,
                key="API_PORT",
                label="API news",
                help="Puerto del panel y la API en el host.",
                kind="text",
                default="8000",
                required=True,
            ),
            Field(
                file=DEPLOY,
                key="GATEWAY_PORT",
                label="Puerto gateway",
                help="Puerto del TTS gateway en el host.",
                kind="text",
                default="8001",
                required=True,
            ),
        ),
    ),
    Section(
        file=DEPLOY,
        title="Seguridad",
        fields=(
            Field(
                file=DEPLOY,
                key="GATEWAY_API_KEY",
                label="Clave del gateway",
                help="Clave X-API-Key compartida (Ctrl+G la genera). "
                "Vacío deja el gateway abierto en la red.",
                kind="secret",
                generate="hex32",
            ),
        ),
    ),
    Section(
        file=DEPLOY,
        title="Despliegue",
        fields=(
            Field(
                file=DEPLOY,
                key="COMPOSE_PROFILES",
                label="Perfiles compose",
                help="Shape A (un host) = news,gateway · Shape B = news (app) o gateway (GPU).",
                kind="choice",
                default="news,gateway",
                options=("news,gateway", "news", "gateway"),
            ),
            Field(
                file=DEPLOY,
                key="QWEN3_TTS_REMOTE_URL",
                label="URL sidecar qwen3",
                help="Vacío = qwen3 desactivado. Con GPU: "
                "http://gateway-qwen3:8000/api/v1 y QWEN3=1 al desplegar.",
                default="",
                advanced=True,
            ),
        ),
    ),
    Section(
        file=NEWS,
        title="Acceso al panel (admin)",
        fields=(
            Field(
                file=NEWS,
                key="ADMIN_USERNAME",
                label="Usuario admin",
                help="Recomendado: usuario de administrador (usa el hash bcrypt de al lado).",
                default="",
            ),
            Field(
                file=NEWS,
                key="ADMIN_PASSWORD_HASH",
                label="Hash admin",
                help="Enter abre un diálogo que genera el hash bcrypt "
                "con una contraseña que no se guarda.",
                kind="password",
                default="",
                advanced=True,
            ),
            Field(
                file=NEWS,
                key="ADMIN_API_TOKEN",
                label="Token admin",
                help="Alternativa legacy al usuario+hash: un único token para REST y GUI.",
                kind="secret",
                generate="hex32",
            ),
        ),
    ),
    Section(
        file=NEWS,
        title="LLM",
        fields=(
            Field(
                file=NEWS,
                key="LLM_API_BASE",
                label="Base LLM",
                help="Endpoint OpenAI-compatible de llama.cpp (en Docker: host.docker.internal).",
                default="http://host.docker.internal:8080/v1",
                required=True,
            ),
            Field(
                file=NEWS,
                key="LLM_MODEL",
                label="Modelo LLM",
                help="Modelo servido por ese endpoint.",
                default="openai/local-model",
            ),
        ),
    ),
    Section(
        file=NEWS,
        title="Entrega de boletines",
        fields=(
            Field(
                file=NEWS,
                key="TELEGRAM_CHAT_ID",
                label="Chat Telegram",
                help="Chat de destino; vacío desactiva la entrega por Telegram.",
                default="",
            ),
            Field(
                file=NEWS,
                key="TELEGRAM_BOT_TOKEN",
                label="Token Telegram",
                help="Token de @BotFather; no se puede generar automáticamente.",
                kind="secret",
                default="",
            ),
            Field(
                file=NEWS,
                key="SMTP_HOST",
                label="SMTP host",
                help="Servidor SMTP para entrega y códigos de acceso.",
                default="smtp.gmail.com",
                advanced=True,
            ),
            Field(
                file=NEWS,
                key="SMTP_PORT",
                label="SMTP puerto",
                help="Puerto SMTP (587 con STARTTLS en Gmail).",
                default="587",
                advanced=True,
            ),
            Field(
                file=NEWS,
                key="SMTP_USER",
                label="SMTP usuario",
                help="Usuario SMTP; vacío desactiva el correo.",
                default="",
                advanced=True,
            ),
            Field(
                file=NEWS,
                key="SMTP_PASSWORD",
                label="SMTP contraseña",
                help="Contraseña de aplicación de Gmail, nunca la contraseña de la cuenta.",
                kind="secret",
                default="",
                advanced=True,
            ),
        ),
    ),
    Section(
        file=NEWS,
        title="TTS y gateway",
        fields=(
            Field(
                file=NEWS,
                key="TTS_API_BASE_URL",
                label="URL gateway",
                help="En Docker usa http://gateway:8000/api/v1 (DNS interno, no el puerto 8001).",
                default="http://gateway:8000/api/v1",
                required=True,
            ),
            Field(
                file=NEWS,
                key="TTS_API_KEY",
                label="Clave gateway",
                help="Se copia sola de deploy/.env cuando generas la clave compartida.",
                kind="secret",
                generate="hex32",
            ),
            Field(
                file=NEWS,
                key="TTS_DEFAULT_BACKEND",
                label="Motor por defecto",
                help="Motor de los digests; edge no necesita gateway, el resto sí.",
                kind="choice",
                default="edge",
                options=(
                    "edge",
                    "omni_voice",
                    "qwen3_tts",
                    "kokoro",
                    "fish_tts",
                    "openai_tts",
                    "zonos2",
                    "voxcpm2",
                ),
            ),
            Field(
                file=NEWS,
                key="TTS_LANGUAGE",
                label="Idioma TTS",
                help="Idioma por defecto de la síntesis y de los prompts.",
                default="es",
            ),
            Field(
                file=NEWS,
                key="TTS_VOICE",
                label="Voz TTS",
                help="Cualquier ShortName de Edge neural (p. ej. es-MX-JorgeNeural).",
                default="es-MX-JorgeNeural",
            ),
        ),
    ),
    Section(
        file=NEWS,
        title="Audio y enlaces firmados",
        fields=(
            Field(
                file=NEWS,
                key="DOC_TTS_LINK_SECRET",
                label="Secreto enlaces",
                help="HMAC de los enlaces de audio; sin él el subido de documentos responde 503.",
                kind="secret",
                generate="hex32",
            ),
            Field(
                file=NEWS,
                key="API_PORT",
                label="Puerto API",
                help="Debe coincidir con deploy/API_PORT.",
                default="8000",
                required=True,
            ),
        ),
    ),
    Section(
        file=GATEWAY,
        title="Motor de síntesis",
        fields=(
            Field(
                file=GATEWAY,
                key="TTS_ENGINE",
                label="Engine",
                help="routed reparte por idioma; los engines GPU/cloud requieren "
                "su sidecar o credenciales.",
                kind="choice",
                default="routed",
                options=ENGINE_OPTIONS,
                required=True,
            ),
            Field(
                file=GATEWAY,
                key="TTS_ENGINES",
                label="Mapa idioma→engine",
                help="Ruta por idioma cuando TTS_ENGINE=routed (JSON). "
                "El asistente TTS ofrece presets y un editor.",
                default=_TTS_ENGINES,
            ),
            Field(
                file=GATEWAY,
                key="TTS_EXTRA_ENGINES",
                label="Engines extra",
                help="Pins explícitos sin rutear idioma; marca varios. "
                "Añade fish_tts/openai_tts solo con credenciales.",
                kind="multi",
                default="kokoro,qwen3_tts,omnivoice_remote",
                options=EXTRA_ENGINE_OPTIONS,
            ),
            Field(
                file=GATEWAY,
                key="TTS_MODEL_DOWNLOADS",
                label="Modelos a descargar",
                help="Checkpoints locales que quieres precargar; marca varios. "
                "No incluye engines cloud.",
                kind="multi",
                default="",
                options=MODEL_OPTIONS,
            ),
        ),
    ),
    Section(
        file=GATEWAY,
        title="GPU y sidecars",
        fields=(
            Field(
                file=GATEWAY,
                key="TTS_DEVICE",
                label="Dispositivo",
                help="cuda:0 exige el overlay GPU; cpu funciona sin gráfica.",
                kind="choice",
                default="cuda:0",
                options=("cuda:0", "cpu"),
            ),
            Field(
                file=GATEWAY,
                key="OMNIVOICE_REMOTE_URL",
                label="URL sidecar",
                help="Sidecar OmniVoice; vacío mantiene el proxy arrancado pero inactivo.",
                default="http://gateway-omnivoice:8000",
                advanced=True,
            ),
            Field(
                file=GATEWAY,
                key="QWEN3_TTS_REMOTE_URL",
                label="URL sidecar qwen3 (dev)",
                help="Solo desarrollo local; en Docker se toma de deploy/.env.",
                default="",
                advanced=True,
            ),
            Field(
                file=GATEWAY,
                key="QWEN3_TTS_REMOTE_TIMEOUT",
                label="Timeout qwen3",
                help="Segundos de espera del proxy qwen3 (carga inicial incluida).",
                default="300",
                advanced=True,
            ),
        ),
    ),
    Section(
        file=GATEWAY,
        title="Proveedores cloud",
        fields=(
            Field(
                file=GATEWAY,
                key="FISH_API_KEY",
                label="Fish API key",
                help="Vacío desactiva Fish; actívalo con "
                "TTS_EXTRA_ENGINES=fish_tts o un pin engine=fish_tts.",
                kind="secret",
                default="",
            ),
            Field(
                file=GATEWAY,
                key="FISH_MODEL",
                label="Modelo Fish",
                help="Modelo de síntesis de Fish Audio.",
                default="s2.1-pro-free",
            ),
            Field(
                file=GATEWAY,
                key="FISH_API_BASE_URL",
                label="Base Fish",
                help="Endpoint de la API de Fish Audio.",
                default="https://api.fish.audio",
                advanced=True,
            ),
            Field(
                file=GATEWAY,
                key="OPENAI_TTS_BASE_URL",
                label="Base OpenAI TTS",
                help="Incluye /v1; vacío desactiva el proveedor OpenAI-compatible.",
                default="",
                advanced=True,
            ),
            Field(
                file=GATEWAY,
                key="OPENAI_TTS_API_KEY",
                label="OpenAI TTS key",
                help="Bearer opcional del endpoint OpenAI-compatible.",
                kind="secret",
                default="",
                advanced=True,
            ),
            Field(
                file=GATEWAY,
                key="OPENAI_TTS_MODEL",
                label="Modelo OpenAI TTS",
                help="Modelo enviado a /audio/speech.",
                default="tts-1",
                advanced=True,
            ),
        ),
    ),
    Section(
        file=GATEWAY,
        title="Seguridad y HuggingFace",
        fields=(
            Field(
                file=GATEWAY,
                key="API_KEY",
                label="API key",
                help="Misma clave que deploy/GATEWAY_API_KEY (se copia sola).",
                kind="secret",
                generate="hex32",
            ),
            Field(
                file=GATEWAY,
                key="HF_TOKEN",
                label="Token HF",
                help="Token de HuggingFace para el clonado de voces del repo gated (opcional).",
                kind="secret",
                default="",
                advanced=True,
            ),
        ),
    ),
]

SECTIONS: tuple[Section, ...] = tuple(_SECTIONS)


def sections_for(file_key: str) -> list[Section]:
    """Return the sections that belong to one env file.

    Args:
        file_key: ``deploy``, ``news`` or ``gateway``.

    Returns:
        Sections in display order.
    """
    return [section for section in SECTIONS if section.file == file_key]


def fields_for(file_key: str) -> list[Field]:
    """Return every field of one env file, ignoring section headings.

    Args:
        file_key: ``deploy``, ``news`` or ``gateway``.

    Returns:
        Fields in display order.
    """
    return [field_ for section in sections_for(file_key) for field_ in section.fields]


# -- profiles ---------------------------------------------------------------

_URL_GW_CONTAINER = "http://gateway:8000/api/v1"
_URL_GW_HOST = "http://localhost:8001/api/v1"

PROFILES: tuple[Profile, ...] = (
    Profile(
        key="docker",
        label="Docker · Shape A (todo en un host)",
        description="Levanta las 13 con `make up`. El gateway se llama por DNS interno.",
        defaults={
            (DEPLOY, "COMPOSE_PROFILES"): "news,gateway",
            (NEWS, "LLM_API_BASE"): "http://host.docker.internal:8080/v1",
            (NEWS, "TTS_API_BASE_URL"): _URL_GW_CONTAINER,
        },
    ),
    Profile(
        key="local",
        label="Desarrollo local (sin Docker)",
        description="Virtualenvs + procesos en el host: API :8000 y gateway :8001.",
        defaults={
            (NEWS, "LLM_API_BASE"): "http://localhost:8080/v1",
            (NEWS, "TTS_API_BASE_URL"): _URL_GW_HOST,
        },
    ),
    Profile(
        key="split",
        label="Docker · Shape B (app + GPU separadas)",
        description="Perfil news en el host de la app y gateway en el host con GPU.",
        defaults={
            (DEPLOY, "COMPOSE_PROFILES"): "news",
            (NEWS, "LLM_API_BASE"): "http://host.docker.internal:8080/v1",
            (NEWS, "TTS_API_BASE_URL"): _URL_GW_HOST,
        },
    ),
    Profile(
        key="audit",
        label="Solo revisar lo que ya existe",
        description="No reescribe nada: valida los .env actuales y las dependencias.",
        defaults={},
    ),
)


def profiles() -> list[Profile]:
    """Return the selectable starting profiles."""
    return list(PROFILES)
