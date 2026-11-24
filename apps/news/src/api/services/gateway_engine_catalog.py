"""Shared view of the TTS engine catalog and its availability for the news UI."""

from __future__ import annotations

import time
from dataclasses import dataclass

from tts_contract import EnginesResponse, canonical_engine_id

from src.api.models.narration import TTSBackend
from src.api.services.voices import VoicesService

GATEWAY_ENGINE_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "(engine activo del gateway)"),
    ("omnivoice", "OmniVoice (local)"),
    ("omnivoice_remote", "OmniVoice remote (sidecar)"),
    ("pocket_tts", "Pocket TTS"),
    ("edgetts", "EdgeTTS (gateway)"),
    ("piper", "Piper"),
    ("chatterbox", "Chatterbox"),
    ("qwen3_tts", "Qwen3-TTS (gateway)"),
    ("zono2", "ZONOS2"),
    ("kokoro", "Kokoro"),
    ("fish_tts", "Fish Audio"),
    ("openai_tts", "OpenAI-compatible"),
)

_HIDDEN_ENGINES = frozenset({"routed", "mock"})
_CLOUD_BACKENDS = frozenset({TTSBackend.FISH.value, TTSBackend.OPENAI.value})
_ENGINE_CATALOG_CACHE: dict[str, tuple[float, GatewayEngineCatalog]] = {}
_ENGINE_CATALOG_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class GatewayEngineCatalog:
    """Catalog snapshot used to build engine selectors.

    Attributes:
        active: Engine currently active in the gateway.
        engines: Descriptors reported by ``GET /engines``.
        fetched_ok: False when the gateway could not be reached or returned no engines.
    """

    active: str
    engines: tuple[object, ...]
    fetched_ok: bool

    @property
    def available_names(self) -> set[str]:
        """Names of engines the gateway reports as ready."""
        return {
            canonical_engine_id(engine.name)
            for engine in self.engines
            if getattr(engine, "available", False)
        }


@dataclass(frozen=True)
class BackendAvailability:
    """Whether a news-engine backend can render right now."""

    available: bool
    reason: str = ""


class BackendUnavailableError(ValueError):
    """Raised when a submit or render asks for an unavailable engine."""


def reset_gateway_engine_catalog_cache() -> None:
    """Drop the shared engine-catalog snapshot."""
    _ENGINE_CATALOG_CACHE.clear()


async def load_gateway_engine_catalog(
    service: VoicesService | None = None,
    *,
    force: bool = False,
) -> GatewayEngineCatalog:
    """Load one catalog snapshot, using a short-lived shared cache.

    Args:
        service: Optional injected client, mainly for tests.
        force: Bypass the shared cache for this load.

    Returns:
        Catalog snapshot with ``fetched_ok=False`` when the static fallback applies.
    """
    if service is None and not force:
        cached = _ENGINE_CATALOG_CACHE.get("default")
        if cached is not None and time.monotonic() - cached[0] <= _ENGINE_CATALOG_TTL_SECONDS:
            return cached[1]
    try:
        data = await (service or VoicesService()).get_engines()
        catalog = EnginesResponse.model_validate(data)
    except Exception:
        result = GatewayEngineCatalog(active="", engines=(), fetched_ok=False)
    else:
        if not catalog.engines:
            result = GatewayEngineCatalog(active=catalog.active, engines=(), fetched_ok=False)
        else:
            result = GatewayEngineCatalog(
                active=canonical_engine_id(catalog.active),
                engines=tuple(catalog.engines),
                fetched_ok=True,
            )
    if service is None and result.fetched_ok:
        _ENGINE_CATALOG_CACHE["default"] = (time.monotonic(), result)
    return result


def _descriptor_for(catalog: GatewayEngineCatalog, name: str):
    """Return the descriptor of one canonical gateway engine id."""
    key = canonical_engine_id(name)
    for engine in catalog.engines:
        if canonical_engine_id(getattr(engine, "name", "")) == key:
            return engine
    return None


def gateway_engine_availability(
    catalog: GatewayEngineCatalog,
    name: str,
) -> BackendAvailability:
    """Report whether one gateway sub-engine can be pinned."""
    key = canonical_engine_id(name)
    if not key:
        return BackendAvailability(False, "engine del gateway no especificado")
    if not catalog.fetched_ok:
        return BackendAvailability(False, "catálogo del gateway no disponible")
    descriptor = _descriptor_for(catalog, key)
    if descriptor is None:
        return BackendAvailability(False, f"engine {key} no está en el catálogo del gateway")
    if not getattr(descriptor, "available", False):
        reason = getattr(descriptor, "error", "") or "engine no disponible"
        return BackendAvailability(False, reason)
    return BackendAvailability(True)


def backend_availability(
    catalog: GatewayEngineCatalog,
    settings: object | None = None,
) -> dict[str, BackendAvailability]:
    """Report availability for every public news-engine backend."""
    from src.core.config import get_settings

    current = settings or get_settings()
    modal_ready = bool(
        current.modal_enabled and current.modal_token_id and current.modal_token_secret
    )
    result: dict[str, BackendAvailability] = {}
    for backend in TTSBackend:
        name = backend.value
        if backend is TTSBackend.EDGE:
            result[name] = BackendAvailability(True)
        elif backend is TTSBackend.GATEWAY:
            if not catalog.fetched_ok:
                result[name] = BackendAvailability(False, "catálogo del gateway no disponible")
            elif catalog.active:
                status = gateway_engine_availability(catalog, catalog.active)
                result[name] = (
                    status
                    if status.available
                    else BackendAvailability(
                        False, f"engine activo {catalog.active}: {status.reason}"
                    )
                )
            elif any(getattr(engine, "available", False) for engine in catalog.engines):
                result[name] = BackendAvailability(True)
            else:
                result[name] = BackendAvailability(False, "ningún engine del gateway está listo")
        elif backend in (
            TTSBackend.OMNIVOICE,
            TTSBackend.KOKORO,
            TTSBackend.FISH,
            TTSBackend.OPENAI,
        ):
            result[name] = gateway_engine_availability(catalog, name)
        elif backend is TTSBackend.QWEN3_TTS:
            gateway_status = gateway_engine_availability(catalog, name)
            if gateway_status.available or modal_ready:
                result[name] = BackendAvailability(True)
            else:
                result[name] = BackendAvailability(False, "Qwen3-TTS local y Modal no disponibles")
        elif backend is TTSBackend.ZONOS2:
            if not current.modal_enabled:
                result[name] = BackendAvailability(False, "Modal está desactivado")
            elif not current.zonos2_enabled:
                result[name] = BackendAvailability(False, "ZONO2 está detrás del gate de benchmark")
            elif not modal_ready:
                result[name] = BackendAvailability(False, "credenciales de Modal no configuradas")
            else:
                result[name] = BackendAvailability(True)
        elif backend is TTSBackend.VOXCPM2:
            if not current.modal_enabled:
                result[name] = BackendAvailability(False, "Modal está desactivado")
            elif not current.voxcpm2_enabled:
                result[name] = BackendAvailability(
                    False, "VoxCPM2 está detrás del gate de benchmark"
                )
            elif not modal_ready:
                result[name] = BackendAvailability(False, "credenciales de Modal no configuradas")
            else:
                result[name] = BackendAvailability(True)
    return result


async def assert_backend_available(
    backend: str,
    *,
    gateway_engine: str = "",
    settings: object | None = None,
    strict: bool = True,
) -> BackendAvailability:
    """Check one backend (and its gateway pin) before a submit or a render.

    Args:
        backend: Public backend id, legacy spellings accepted.
        gateway_engine: Optional gateway sub-engine pin.
        settings: Injected settings, mainly for tests.
        strict: When False, an unreachable catalog is treated as unknown instead
            of unavailable, so an in-flight render is not rejected by an outage.

    Returns:
        The backend's availability.

    Raises:
        BackendUnavailableError: If the engine cannot render right now.
    """
    catalog = await load_gateway_engine_catalog()
    name = canonical_engine_id(backend)
    status = backend_availability(catalog, settings=settings).get(
        name, BackendAvailability(False, f"backend desconocido {backend!r}")
    )
    if (strict or catalog.fetched_ok) and not status.available:
        raise BackendUnavailableError(status.reason or f"{name} no disponible")
    if gateway_engine and name in (TTSBackend.OMNIVOICE.value, TTSBackend.GATEWAY.value):
        pin_status = gateway_engine_availability(catalog, gateway_engine)
        if (strict or catalog.fetched_ok) and not pin_status.available:
            raise BackendUnavailableError(pin_status.reason or f"{gateway_engine} no disponible")
    return status


def engine_label(name: str, catalog: GatewayEngineCatalog, selected: str = "") -> str:
    """Return the display label of one gateway engine id.

    Args:
        name: Engine id.
        catalog: Current catalog snapshot.
        selected: Currently selected id, preserved even when unavailable.

    Returns:
        Human-readable label with a warning suffix when unavailable.
    """
    key = canonical_engine_id(name)
    descriptor = _descriptor_for(catalog, key)
    label = getattr(descriptor, "label", None) or dict(GATEWAY_ENGINE_CHOICES).get(key, key)
    status = gateway_engine_availability(catalog, key)
    available = status.available or key == selected or key == catalog.active
    return label if available else f"{label} (no disponible)"


def gateway_engine_choices(
    catalog: GatewayEngineCatalog,
    *,
    selected: str = "",
) -> tuple[tuple[str, str], ...]:
    """Build the gateway-engine choices from a catalog snapshot.

    Args:
        catalog: Current catalog snapshot.
        selected: Persisted or submitted value that must stay visible.

    Returns:
        ``(value, label)`` pairs including the empty "gateway active" option.
    """
    if not catalog.fetched_ok:
        return GATEWAY_ENGINE_CHOICES
    choices: list[tuple[str, str]] = [GATEWAY_ENGINE_CHOICES[0]]
    seen: set[str] = {""}
    for engine in catalog.engines:
        name = canonical_engine_id(getattr(engine, "name", ""))
        if not name or name in seen:
            continue
        if name in _HIDDEN_ENGINES and name != selected:
            continue
        seen.add(name)
        choices.append((name, engine_label(name, catalog, selected)))
    active = canonical_engine_id(catalog.active)
    if active and active not in seen and active not in _HIDDEN_ENGINES:
        choices.append((active, engine_label(active, catalog, selected)))
    if selected and selected not in seen:
        key = canonical_engine_id(selected)
        choices.append((key, engine_label(key, catalog, key)))
    return tuple(choices)


def backend_choices(
    catalog: GatewayEngineCatalog,
    choices: tuple[tuple[str, str], ...],
    settings: object | None = None,
) -> tuple[tuple[str, str], ...]:
    """Mark unavailable backends and hide positively unavailable cloud ones.

    Args:
        catalog: Current catalog snapshot.
        choices: Backend pairs in menu order.
        settings: Optional injected settings.

    Returns:
        Visible backend pairs; the full list is kept during a gateway outage.
    """
    availability = backend_availability(catalog, settings=settings)
    visible: list[tuple[str, str]] = []
    for value, label in choices:
        key = canonical_engine_id(value)
        status = availability.get(key)
        if status is not None and not status.available:
            if catalog.fetched_ok and key in _CLOUD_BACKENDS:
                continue
            label = f"{label} (no disponible)"
        visible.append((key, label))
    return tuple(visible)
