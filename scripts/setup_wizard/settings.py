"""Curated configuration model: which keys the wizard asks for, and why.

Every screen in the assistant is a projection of :data:`SECTIONS`, so the
curses renderer and the non interactive fallback share one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Field",
    "Profile",
    "PROFILES",
    "SECTIONS",
    "fields_for",
    "hash_password",
    "profiles",
    "sections_for",
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
        kind: ``text``, ``secret``, ``choice``, ``bool`` or ``password``.
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


# -- fields -----------------------------------------------------------------

DEPLOY = "deploy"
NEWS = "news"
GATEWAY = "gateway"

_TTS_ENGINES = (
    '{"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}'
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
                key="TTS_PATH",
                label="Ruta OmniVoice",
                help="Instalación externa montada en el gateway (perfil GPU).",
                default="/opt/omnivoice",
                advanced=True,
            ),
            Field(
                file=DEPLOY,
                key="QWEN3_TTS_DIR",
                label="Ruta qwen3",
                help="venv externo de qwen3_tts; solo tiene sentido con el overlay GPU.",
                default="/opt/qwen3-tts",
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
                help="Motor de síntesis de los digests; edge no necesita gateway.",
                kind="choice",
                default="edge",
                options=("edge", "omni_voice", "qwen3_tts", "zonos2", "voxcpm2"),
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
                help="routed reparte es/en/fr por idioma; mock solo genera tonos de prueba.",
                kind="choice",
                default="routed",
                options=("routed", "pocket_tts", "edgetts", "kokoro", "omnivoice", "mock"),
                required=True,
            ),
            Field(
                file=GATEWAY,
                key="TTS_ENGINES",
                label="Mapa idioma→engine",
                help="Ruta por idioma cuando TTS_ENGINE=routed (JSON).",
                default=_TTS_ENGINES,
            ),
            Field(
                file=GATEWAY,
                key="TTS_EXTRA_ENGINES",
                label="Engines extra",
                help="Engines disponibles además de la ruta (pins explícitos engine=...).",
                default="kokoro,qwen3_tts,omnivoice_remote",
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
