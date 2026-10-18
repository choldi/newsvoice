"""Shared Pydantic models for the tts-api gateway contract.

Both apps in the monorepo import this package: ``apps/gateway`` (``tts_api``)
declares its JSON responses with these models, and ``apps/news`` parses gateway
answers with them instead of hand-reading dicts. Keeping one definition is what
stops the two sides from drifting apart.
"""

from __future__ import annotations

from tts_contract.catalog import (
    STATUS_GENERATED,
    STATUS_PENDING,
    STATUS_STALE,
    VoiceCatalogError,
    VoiceCatalogResponse,
    VoiceManifestEntry,
    VoiceProfile,
    VoiceSource,
    classify_status,
    is_stale,
    is_voice_slug,
    load_catalog_profiles,
    missing_or_stale,
    sha256_bytes,
    sha256_text,
)
from tts_contract.engines import EngineCapabilities, EngineDescriptor, EnginesResponse
from tts_contract.errors import (
    GatewayErrorBody,
    GatewayErrorDetail,
    GatewayErrorInfo,
    TokenSuggestion,
    parse_gateway_error,
)
from tts_contract.requests import ConversationRequest, ConversationTurn, TTSRequest
from tts_contract.system import DiskUsage, HealthResponse
from tts_contract.voices import (
    ClonedVoice,
    CloneVoiceResponse,
    DesignedVoice,
    DesignedVoiceResponse,
    StockVoice,
)

__version__ = "0.1.0"

__all__ = [
    "STATUS_GENERATED",
    "STATUS_PENDING",
    "STATUS_STALE",
    "CloneVoiceResponse",
    "ClonedVoice",
    "ConversationRequest",
    "ConversationTurn",
    "DesignedVoice",
    "DesignedVoiceResponse",
    "DiskUsage",
    "EngineCapabilities",
    "EngineDescriptor",
    "EnginesResponse",
    "GatewayErrorBody",
    "GatewayErrorDetail",
    "GatewayErrorInfo",
    "HealthResponse",
    "StockVoice",
    "TTSRequest",
    "TokenSuggestion",
    "VoiceCatalogError",
    "VoiceCatalogResponse",
    "VoiceManifestEntry",
    "VoiceProfile",
    "VoiceSource",
    "__version__",
    "classify_status",
    "is_stale",
    "is_voice_slug",
    "load_catalog_profiles",
    "missing_or_stale",
    "parse_gateway_error",
    "sha256_bytes",
    "sha256_text",
]
