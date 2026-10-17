"""The shared tts-contract package: one definition of the gateway's JSON.

The digest path, the narration renderer and the monitoring checks all read
gateway answers through these models, so a shape change breaks one test file
instead of three hand-written dict readers.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tts_contract import (
    ClonedVoice,
    CloneVoiceResponse,
    DiskUsage,
    EngineDescriptor,
    EnginesResponse,
    HealthResponse,
    StockVoice,
    parse_gateway_error,
)

# --- gateway error unwrapping ------------------------------------------------


def test_parse_nested_error_body() -> None:
    """The nested ``detail`` object carries the type and suggested voices."""
    info = parse_gateway_error(
        {
            "detail": {
                "detail": "nope",
                "error_type": "voice_not_found",
                "valid_voice_ids": ["a", "b"],
            }
        },
        status_code=404,
    )

    assert info.error_type == "voice_not_found"
    assert info.valid_voice_ids == ["a", "b"]
    assert "valid voice ids: a, b" in info.message


def test_parse_flat_detail() -> None:
    """A plain string detail is the message verbatim."""
    info = parse_gateway_error({"detail": "unsupported_language"}, status_code=400)

    assert info.message == "unsupported_language"
    assert info.error_type == ""


def test_parse_middleware_error_type_is_top_level() -> None:
    """The auth middleware puts ``error_type`` beside ``detail``, not inside."""
    info = parse_gateway_error(
        {"detail": "API key invalida", "error_type": "unauthorized"}, status_code=401
    )

    assert info.message == "API key invalida"
    assert info.error_type == "unauthorized"


def test_parse_rate_limit_error_has_no_detail() -> None:
    """The slowapi 429 answers with an ``error`` key only."""
    info = parse_gateway_error({"error": "Rate limit exceeded: 10/hour"}, status_code=429)

    assert info.message == "Rate limit exceeded: 10/hour"
    assert info.error_type == ""


def test_parse_non_object_body_falls_back_to_the_status() -> None:
    """An HTML error page or a missing body must not raise."""
    assert parse_gateway_error(["boom"], status_code=502).message == "gateway returned 502"
    assert parse_gateway_error(None, status_code=502).message == "gateway returned 502"


def test_parse_suggests_at_most_eight_voice_ids() -> None:
    """Only the first eight suggestions reach the human-readable message."""
    ids = [f"v{index}" for index in range(10)]

    info = parse_gateway_error({"detail": {"detail": "nope", "valid_voice_ids": ids}})

    assert ", ".join(ids[:8]) in info.message
    assert ids[8] not in info.message


# --- voices ------------------------------------------------------------------


def test_stock_voice_tolerates_extra_keys() -> None:
    """Unknown keys are ignored rather than breaking the caller."""
    voice = StockVoice.model_validate({"voice_id": "es-mx-male", "extra": 1})

    assert voice.voice_id == "es-mx-male"
    assert voice.language == ""


def test_cloned_voice_is_keyed_by_id() -> None:
    """Stored clones are addressed by ``id`` and a comma-separated ``engine``."""
    voice = ClonedVoice.model_validate(
        {"id": "uuid", "name": "x", "engine": "pocket_tts,omnivoice", "metadata": {"k": 1}}
    )

    assert voice.id == "uuid"
    assert voice.engine == "pocket_tts,omnivoice"
    assert voice.metadata == {"k": 1}


def test_clone_response_is_keyed_by_voice_id() -> None:
    """A fresh clone answers with ``voice_id``, unlike a stored row."""
    response = CloneVoiceResponse.model_validate({"voice_id": "uuid"})

    assert response.voice_id == "uuid"
    assert response.ref_text is None


# --- system ------------------------------------------------------------------


def test_disk_usage_requires_total_and_free() -> None:
    """A malformed disk payload is rejected instead of reading as an empty disk."""
    with pytest.raises(ValidationError):
        DiskUsage.model_validate({"unexpected": 1})

    disk = DiskUsage.model_validate({"total": 10, "free": 5})

    assert (disk.path, disk.used) == ("", 0)


def test_health_defaults_for_the_not_initialized_shape() -> None:
    """The 503 body only carries status, version and engine."""
    health = HealthResponse.model_validate({"status": "not_initialized", "version": "0.2.0"})

    assert health.status == "not_initialized"
    assert health.stock_voices_count == 0
    assert health.gpu_available is False


# --- engines -----------------------------------------------------------------


def test_unavailable_engine_descriptor_has_no_capabilities() -> None:
    """An engine that could not be introspected reports why, not capabilities."""
    engine = EngineDescriptor.model_validate({"name": "piper", "available": False, "error": "x"})

    assert engine.capabilities is None
    assert engine.error == "x"


def test_engines_catalog_round_trips() -> None:
    """The catalog payload parses back into the same shape."""
    catalog = EnginesResponse.model_validate(
        {
            "status": "ok",
            "active": "qwen3_tts",
            "engines": [
                {
                    "name": "qwen3_tts",
                    "available": True,
                    "locations": ["local"],
                    "capabilities": {"voice_cloning": True},
                }
            ],
        }
    )

    assert catalog.active == "qwen3_tts"
    assert catalog.engines[0].capabilities is not None
    assert catalog.engines[0].capabilities.voice_cloning is True
