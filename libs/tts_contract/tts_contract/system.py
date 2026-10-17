"""System payloads: ``GET /health`` and ``GET /disk``.

``DiskUsage.total``/``free`` are required so a malformed payload is rejected
rather than silently treated as an empty disk; ``path``/``used`` are optional so
older peers that omit them still parse.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["DiskUsage", "HealthResponse"]


class HealthResponse(BaseModel):
    """The ``GET /health`` body.

    On the ``200`` path every field is present; the ``503`` (engine not
    initialised) path carries only ``status``/``version``/``engine``, hence the
    defaults. A ``degraded`` status is still served with HTTP ``200``.
    """

    model_config = ConfigDict(extra="ignore")

    status: str = "unknown"
    version: str = ""
    engine: str = ""
    device: str = ""
    mode: str = ""
    stock_voices_count: int = 0
    gpu_available: bool = False


class DiskUsage(BaseModel):
    """The ``GET /disk`` body, in bytes."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    total: int
    free: int
    used: int = 0
