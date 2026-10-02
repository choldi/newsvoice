"""Services package initialization.

Submodules resolve lazily so an importer that needs one service does not pull
in every dependency of the package through this file. That matters for the
voice workers: the design box has no database driver, so an eager import of
``cluster``/``digest`` here would make ``voice_staging`` unusable there.
"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ClusterService": "src.api.services.cluster",
    "DedupService": "src.api.services.dedup",
    "DigestService": "src.api.services.digest",
    "normalize_url": "src.api.services.dedup",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load a re-exported service member on first access (PEP 562).

    Args:
        name: Attribute requested on the package.

    Returns:
        The member from the submodule that defines it.

    Raises:
        AttributeError: When the package has no such member, which is what
            lets ``from src.api.services import some_module`` still fall
            through to a submodule import.
    """
    if name in _EXPORTS:
        value = getattr(import_module(_EXPORTS[name]), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
