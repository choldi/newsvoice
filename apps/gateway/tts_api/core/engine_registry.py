from __future__ import annotations

import json
import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_MODULE_PATH = Path(__file__).resolve()
_REPO_CONFIG_PARENT_INDEX = 4
_REPO_CONFIG_PATH = (
    _MODULE_PATH.parents[_REPO_CONFIG_PARENT_INDEX] / "config" / "engines.toml"
    if len(_MODULE_PATH.parents) > _REPO_CONFIG_PARENT_INDEX
    else _MODULE_PATH.parents[-1] / "config" / "engines.toml"
)
_APP_CONFIG_PATH = _MODULE_PATH.parents[2] / "config" / "engines.toml"
DEFAULT_ENGINE_CONFIG_PATH = next(
    (candidate for candidate in (_APP_CONFIG_PATH, _REPO_CONFIG_PATH) if candidate.is_file()),
    _REPO_CONFIG_PATH if _REPO_CONFIG_PATH.parent.is_dir() else _APP_CONFIG_PATH,
)
DEFAULT_ENGINE_OVERRIDE_PATH = DEFAULT_ENGINE_CONFIG_PATH.parent / "engines.local.json"


class EngineKind(StrEnum):
    LOCAL_CLASS = "local_class"
    OPENAI_COMPATIBLE_HTTP = "openai_compatible_http"


class EngineCapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_voices: bool = True
    voice_cloning: bool = False
    instruct_voice_design: bool = False
    emotions: bool = False
    streaming: bool = False
    supported_languages: list[str] = Field(default_factory=list)


class EngineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: EngineKind = EngineKind.LOCAL_CLASS
    display_name: str | None = None
    enabled: bool = True
    locations: list[str] = Field(default_factory=lambda: ["local"])
    required_env: list[str] = Field(default_factory=list)
    required_modules: list[str] = Field(default_factory=list)
    auto_include_when_configured: bool = False
    class_path: str | None = None
    class_path_remote: str | None = None
    remote_url_env: str | None = None
    capabilities: EngineCapabilitySpec = Field(default_factory=EngineCapabilitySpec)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _local_class_has_class_path(self) -> EngineSpec:
        if self.kind is EngineKind.LOCAL_CLASS and not self.class_path:
            raise ValueError("class_path es obligatorio para engines local_class")
        if self.kind is EngineKind.OPENAI_COMPATIBLE_HTTP and not self.options:
            raise ValueError("options es obligatorio para engines openai_compatible_http")
        return self


class EngineSpecPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EngineKind | None = None
    display_name: str | None = None
    enabled: bool | None = None
    locations: list[str] | None = None
    required_env: list[str] | None = None
    required_modules: list[str] | None = None
    auto_include_when_configured: bool | None = None
    class_path: str | None = None
    class_path_remote: str | None = None
    remote_url_env: str | None = None
    capabilities: EngineCapabilitySpec | None = None
    options: dict[str, Any] | None = None


class EngineRegistry:
    """Declarative catalog of TTS engines with a staged local override."""

    def __init__(
        self,
        base_path: Path | None = None,
        override_path: Path | None = None,
    ) -> None:
        self.base_path = Path(base_path or DEFAULT_ENGINE_CONFIG_PATH)
        self.override_path = Path(override_path or DEFAULT_ENGINE_OVERRIDE_PATH)
        self._base_specs: dict[str, EngineSpec] = {}
        self._override: dict[str, Any] = {"version": 1, "active": None, "engines": {}}
        self._specs: dict[str, EngineSpec] = {}
        self.reload()

    @classmethod
    def load(
        cls,
        base_path: Path | None = None,
        override_path: Path | None = None,
    ) -> EngineRegistry:
        """Load a registry using explicit paths or environment overrides."""
        resolved_base = Path(
            base_path or os.getenv("TTS_ENGINE_REGISTRY_PATH") or DEFAULT_ENGINE_CONFIG_PATH
        )
        resolved_override = Path(
            override_path or os.getenv("TTS_ENGINE_OVERRIDE_PATH") or DEFAULT_ENGINE_OVERRIDE_PATH
        )
        return cls(resolved_base, resolved_override)

    def reload(self) -> None:
        """Read the manifest and the staged override from disk."""
        self._base_specs = self._read_base()
        self._override = self._read_override()
        self._specs = self._merge_specs()

    def _read_base(self) -> dict[str, EngineSpec]:
        if not self.base_path.is_file():
            return {}
        with self.base_path.open("rb") as handle:
            data = tomllib.load(handle)
        raw_engines = data.get("engines")
        if not isinstance(raw_engines, dict):
            return {}
        specs: dict[str, EngineSpec] = {}
        for raw_name, raw_spec in raw_engines.items():
            name = str(raw_name).strip().lower()
            if not name or not isinstance(raw_spec, dict):
                continue
            specs[name] = EngineSpec.model_validate({"name": name, **raw_spec})
        return specs

    def _read_override(self) -> dict[str, Any]:
        if not self.override_path.is_file():
            return {"version": 1, "active": None, "engines": {}}
        try:
            data = json.loads(self.override_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "active": None, "engines": {}}
        if not isinstance(data, dict):
            return {"version": 1, "active": None, "engines": {}}
        engines = data.get("engines")
        if not isinstance(engines, dict):
            engines = {}
        return {
            "version": int(data.get("version", 1)),
            "active": data.get("active"),
            "engines": engines,
        }

    def _merge_specs(self) -> dict[str, EngineSpec]:
        specs = dict(self._base_specs)
        for raw_name, raw_patch in self._override["engines"].items():
            name = str(raw_name).strip().lower()
            if not name or not isinstance(raw_patch, dict):
                continue
            patch = dict(raw_patch)
            patch.pop("name", None)
            current = specs.get(name)
            if current is None:
                candidate: dict[str, Any] = {"name": name, **patch}
            else:
                candidate = current.model_dump()
                candidate.update(patch)
                raw_caps = patch.get("capabilities")
                if isinstance(raw_caps, dict):
                    candidate["capabilities"] = {
                        **current.capabilities.model_dump(),
                        **raw_caps,
                    }
            try:
                specs[name] = EngineSpec.model_validate(candidate)
            except ValidationError:
                if current is not None:
                    specs[name] = current
        return specs

    @property
    def specs(self) -> dict[str, EngineSpec]:
        """Return the effective engine specs keyed by id."""
        return dict(self._specs)

    @property
    def active(self) -> str | None:
        """Return the staged active engine id, if any."""
        active = self._override.get("active")
        return str(active).strip().lower() if active else None

    @property
    def override_data(self) -> dict[str, Any]:
        """Return a copy of the staged override payload."""
        return json.loads(json.dumps(self._override))

    def names(self) -> list[str]:
        """Return all effective engine ids in stable order."""
        return sorted(self._specs)

    def get(self, name: str) -> EngineSpec:
        """Return one effective spec.

        Raises:
            KeyError: When the engine id is not in the catalog.
        """
        key = name.strip().lower()
        try:
            return self._specs[key]
        except KeyError as exc:
            raise KeyError(f"Engine desconocido: {key}") from exc

    def active_name(self, fallback: str) -> str:
        """Return the staged active engine or the supplied fallback."""
        staged = self.active
        if staged and staged in self._specs:
            return staged
        return fallback.strip().lower()

    def candidate(self, name: str, patch: EngineSpecPatch) -> EngineSpec:
        """Build the spec that would result from applying a patch."""
        key = name.strip().lower()
        current = self._specs.get(key)
        raw_patch = patch.model_dump(exclude_unset=True, exclude_none=True)
        raw_patch.pop("name", None)
        if current is None:
            payload: dict[str, Any] = {"name": key, **raw_patch}
        else:
            payload = current.model_dump()
            payload.update(raw_patch)
            raw_caps = raw_patch.get("capabilities")
            if isinstance(raw_caps, dict):
                payload["capabilities"] = {**current.capabilities.model_dump(), **raw_caps}
        return EngineSpec.model_validate(payload)

    def validate(self, name: str, patch: EngineSpecPatch) -> list[str]:
        """Return validation errors for a candidate spec."""
        try:
            self.candidate(name, patch)
        except ValidationError as exc:
            return [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ]
        return []

    def upsert(self, name: str, patch: EngineSpecPatch) -> EngineSpec:
        """Stage a new engine or a partial change to an existing one."""
        errors = self.validate(name, patch)
        if errors:
            raise ValueError("; ".join(errors))
        key = name.strip().lower()
        spec = self.candidate(key, patch)
        raw_patch = patch.model_dump(exclude_unset=True, exclude_none=True, mode="json")
        raw_patch.pop("name", None)
        current_override = self._override["engines"].get(key)
        merged_patch = current_override if isinstance(current_override, dict) else {}
        merged_patch = {**merged_patch, **raw_patch}
        self._override["engines"][key] = merged_patch
        self._specs[key] = spec
        return spec

    def remove(self, name: str) -> bool:
        """Remove a custom engine or disable a manifest engine."""
        key = name.strip().lower()
        if key not in self._specs:
            return False
        if key in self._base_specs:
            self._override["engines"][key] = {"enabled": False}
            self._specs[key] = self._base_specs[key].model_copy(update={"enabled": False})
        else:
            self._override["engines"].pop(key, None)
            self._specs.pop(key, None)
            if self.active == key:
                self._override["active"] = None
        return True

    def set_active(self, name: str) -> None:
        """Stage the engine selected for the next restart."""
        key = name.strip().lower()
        if key not in self._specs:
            raise KeyError(f"Engine desconocido: {key}")
        if not self._specs[key].enabled:
            raise ValueError(f"Engine desactivado: {key}")
        self._override["active"] = key

    def save(self) -> None:
        """Write the staged override payload as JSON."""
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text(
            json.dumps(self._override, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def known_engine_names() -> list[str]:
    """Return engine ids from the default manifest and override."""
    return EngineRegistry.load().names()


def registry_for(settings: Any) -> EngineRegistry:
    """Build a registry from a settings-like object."""
    return EngineRegistry(
        Path(settings.TTS_ENGINE_REGISTRY_PATH),
        Path(settings.TTS_ENGINE_OVERRIDE_PATH),
    )
