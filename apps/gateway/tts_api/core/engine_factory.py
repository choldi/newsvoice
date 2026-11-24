"""Factory for creating TTS engine instances from the declarative registry."""

from __future__ import annotations

import importlib
import json
import logging
import os
from typing import TYPE_CHECKING

from tts_contract import canonical_engine_id

from tts_api.core.engine_base import TtsEngineBase
from tts_api.core.engine_registry import EngineKind, EngineRegistry, registry_for
from tts_api.settings import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _import_engine_class(class_path: str) -> type[TtsEngineBase]:
    module_name, _, class_name = class_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(f"class_path inválido: {class_path}")
    engine_class = getattr(importlib.import_module(module_name), class_name)
    if not isinstance(engine_class, type) or not issubclass(engine_class, TtsEngineBase):
        raise ValueError(f"{class_path} no implementa TtsEngineBase")
    return engine_class


def _create_single_engine(
    name: str,
    registry: EngineRegistry | None = None,
    *,
    require_enabled: bool = True,
) -> TtsEngineBase:
    """Create a single engine instance by name."""
    key = canonical_engine_id(name)
    catalog = registry or registry_for(get_settings())
    try:
        spec = catalog.get(key)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    if require_enabled and not spec.enabled:
        raise ValueError(f"Engine desactivado: {key}")

    if spec.kind is EngineKind.OPENAI_COMPATIBLE_HTTP:
        from tts_api.core.engines.openai_compatible_engine import OpenAICompatibleEngine

        return OpenAICompatibleEngine(spec)

    class_path = spec.class_path or ""
    remote_active = bool(spec.remote_url_env and os.getenv(spec.remote_url_env, "").strip())
    if remote_active and spec.class_path_remote:
        class_path = spec.class_path_remote
    engine_class = _import_engine_class(class_path)
    return engine_class()


def create_engine_for_catalog(name: str, registry: EngineRegistry | None = None) -> TtsEngineBase:
    """Create an engine for catalog introspection without requiring it to be active."""
    return _create_single_engine(name, registry, require_enabled=False)


def _auto_configured_engines(registry: EngineRegistry) -> set[str]:
    names: set[str] = set()
    for spec in registry.specs.values():
        if not spec.enabled or not spec.auto_include_when_configured:
            continue
        env_names = list(spec.required_env)
        if spec.remote_url_env:
            env_names.append(spec.remote_url_env)
        if env_names and all(os.getenv(name, "").strip() for name in env_names):
            names.add(spec.name)
    return names


def _create_routed_engine(
    routing_json: str,
    extra_engines: str = "",
    registry: EngineRegistry | None = None,
) -> TtsEngineBase:
    """Create a RoutedEngine from a JSON routing configuration."""
    from tts_api.core.engines.routed_engine import RoutedEngine

    catalog = registry or registry_for(get_settings())
    if not routing_json or not routing_json.strip():
        raise ValueError(
            "TTS_ENGINE=routed requiere TTS_ENGINES con el mapeo JSON de idiomas. "
            'Ejemplo: TTS_ENGINES=\'{"es":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}\''
        )

    try:
        routing = json.loads(routing_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"TTS_ENGINES no es JSON válido: {e}") from e

    if not isinstance(routing, dict):
        raise ValueError("TTS_ENGINES debe ser un dict JSON {idioma: engine_name}")

    engine_names: set[str] = {str(value) for value in routing.values()}
    engine_names.discard("_default")
    engine_names.update(name.strip() for name in extra_engines.split(",") if name.strip())
    engine_names.update(_auto_configured_engines(catalog))

    routed_names = {str(value) for value in routing.values()}
    optional_engines = engine_names - routed_names

    if not engine_names:
        raise ValueError("TTS_ENGINES no contiene ningún engine válido")

    engines: dict[str, TtsEngineBase] = {}
    for name in engine_names:
        engines[name] = _create_single_engine(name, catalog)

    logger.info(
        "Created routed engine: routing=%s, sub-engines=%s, optional=%s",
        routing,
        list(engines.keys()),
        sorted(optional_engines),
    )

    return RoutedEngine(
        engines=engines,
        routing=routing,
        optional_engines=optional_engines,
    )


def create_engine(engine_name: str | None = None) -> TtsEngineBase:
    """Create and return a TTS engine instance.

    Args:
        engine_name: Engine identifier. If None, uses the effective setting.

    Returns:
        An initialized TtsEngineBase implementation.

    Raises:
        ValueError: If the engine name is not recognized.
    """
    settings = get_settings()
    catalog = registry_for(settings)
    selected = canonical_engine_id(engine_name or settings.effective_tts_engine)

    if selected == "routed":
        return _create_routed_engine(settings.TTS_ENGINES, settings.TTS_EXTRA_ENGINES, catalog)

    return _create_single_engine(selected, catalog)
