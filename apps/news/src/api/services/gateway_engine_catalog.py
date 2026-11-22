"""Shared view of the declarative gateway engine catalog for the news UI."""

from __future__ import annotations

from dataclasses import dataclass

from tts_contract.engines import EngineDescriptor, EnginesResponse

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
_CLOUD_BACKENDS = frozenset({"fish_tts", "openai_tts"})


@dataclass(frozen=True)
class GatewayEngineCatalog:
    """Catalog snapshot used to build engine selectors.

    Attributes:
        active: Engine currently active in the gateway.
        engines: Descriptors reported by ``GET /engines``.
        fetched_ok: False when the gateway could not be reached or returned no engines.
    """

    active: str
    engines: tuple[EngineDescriptor, ...]
    fetched_ok: bool

    @property
    def available_names(self) -> set[str]:
        """Names of engines the gateway reports as ready."""
        return {engine.name for engine in self.engines if engine.available}


async def load_gateway_engine_catalog(service: VoicesService | None = None) -> GatewayEngineCatalog:
    """Load one catalog snapshot, falling back to static choices on failure.

    Args:
        service: Optional injected client, mainly for tests.

    Returns:
        Catalog snapshot with ``fetched_ok=False`` when the static fallback applies.
    """
    try:
        data = await (service or VoicesService()).get_engines()
        catalog = EnginesResponse.model_validate(data)
    except Exception:
        return GatewayEngineCatalog(active="", engines=(), fetched_ok=False)
    if not catalog.engines:
        return GatewayEngineCatalog(active=catalog.active, engines=(), fetched_ok=False)
    return GatewayEngineCatalog(
        active=catalog.active,
        engines=tuple(catalog.engines),
        fetched_ok=True,
    )


def engine_label(name: str, catalog: GatewayEngineCatalog, selected: str = "") -> str:
    """Return the display label of one gateway engine id.

    Args:
        name: Engine id.
        catalog: Current catalog snapshot.
        selected: Currently selected id, preserved even when unavailable.

    Returns:
        Human-readable label with a warning suffix when unavailable.
    """
    label = dict(GATEWAY_ENGINE_CHOICES).get(name, name)
    available = name in catalog.available_names or name == catalog.active or name == selected
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
        name = engine.name.strip().lower()
        if not name or name in seen:
            continue
        if name in _HIDDEN_ENGINES and name != selected:
            continue
        seen.add(name)
        choices.append((name, engine_label(name, catalog, selected)))
    active = catalog.active.strip().lower()
    if active and active not in seen and active not in _HIDDEN_ENGINES:
        choices.append((active, engine_label(active, catalog, selected)))
    if selected and selected not in seen:
        choices.append((selected, engine_label(selected, catalog, selected)))
    return tuple(choices)


def backend_choices(
    catalog: GatewayEngineCatalog,
    choices: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Filter cloud backends the gateway reports as unavailable.

    Args:
        catalog: Current catalog snapshot.
        choices: Backend pairs in menu order.

    Returns:
        Visible backend pairs; the full list is kept during a gateway outage.
    """
    if not catalog.fetched_ok:
        return choices
    available = catalog.available_names
    return tuple(
        choice for choice in choices if choice[0] not in _CLOUD_BACKENDS or choice[0] in available
    )
