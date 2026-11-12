"""Unit tests for the Phase 8.2 digest TTS engine backends."""

import json
from pathlib import Path

import httpx
import pytest

from src.api.models.narration import TTSBackend
from src.api.models.tts import TTSIn
from src.api.services import tts as tts_module
from src.api.services import tts_backends
from src.api.services.tts import TTSService
from src.api.services.tts_backends import (
    ALLOWED_BACKENDS,
    TTSBackendError,
    TTSGatewayError,
    ext_for,
    gateway_headers,
    gateway_voice_for_language,
    is_modal,
    parse_gateway_error,
    resolve_backend,
    synthesize,
    voice_for,
)

# --- resolve_backend ---------------------------------------------------------


def test_resolve_backend_accepts_every_documented_name() -> None:
    """The five engines and their aliases land on the same enum."""
    assert resolve_backend("edge") is TTSBackend.EDGE
    assert resolve_backend("omni_voice") is TTSBackend.OMNIVOICE
    assert resolve_backend("qwen3_tts") is TTSBackend.QWEN3_TTS
    assert resolve_backend("zonos2") is TTSBackend.ZONOS2
    assert resolve_backend("voxcpm2") is TTSBackend.VOXCPM2
    assert resolve_backend("fish_tts") is TTSBackend.FISH
    assert resolve_backend("openai_tts") is TTSBackend.OPENAI


def test_resolve_backend_maps_operator_spellings() -> None:
    """The gateway answers to its new and its old names alike."""
    assert resolve_backend("gateway") is TTSBackend.OMNIVOICE
    assert resolve_backend("tts_api") is TTSBackend.OMNIVOICE
    assert resolve_backend("OmniVoice") is TTSBackend.OMNIVOICE
    assert resolve_backend("edgetts") is TTSBackend.EDGE
    assert resolve_backend("voxcpm") is TTSBackend.VOXCPM2
    assert resolve_backend("fish") is TTSBackend.FISH
    assert resolve_backend("fish_audio") is TTSBackend.FISH
    assert resolve_backend("openai") is TTSBackend.OPENAI


def test_resolve_backend_empty_uses_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No collection value falls through to TTS_DEFAULT_BACKEND."""
    monkeypatch.setattr(tts_backends.get_settings(), "tts_default_backend", "qwen3_tts")

    assert resolve_backend(None) is TTSBackend.QWEN3_TTS
    assert resolve_backend("  ") is TTSBackend.QWEN3_TTS


def test_resolve_backend_rejects_unknown_engine() -> None:
    """A typo fails loudly instead of silently synthesizing with edge."""
    with pytest.raises(TTSBackendError, match="unknown tts backend"):
        resolve_backend("siri")


def test_allowed_backends_covers_the_enum() -> None:
    """The whitelist used by CollectionConfig matches the enum exactly."""
    assert set(ALLOWED_BACKENDS) == {str(member) for member in TTSBackend}


# --- format ------------------------------------------------------------------


def test_ext_for_follows_the_engine() -> None:
    """Edge stores MP3; the gateway and Modal store WAV."""
    assert ext_for(TTSBackend.EDGE) == ".mp3"
    assert ext_for(TTSBackend.OMNIVOICE) == ".wav"
    assert ext_for(TTSBackend.QWEN3_TTS) == ".wav"
    assert ext_for(TTSBackend.ZONOS2) == ".wav"
    assert ext_for(TTSBackend.VOXCPM2) == ".wav"


def test_is_modal_classifies_the_paid_engines() -> None:
    """Only the Modal engines cost money, so only they are gated by the budget."""
    assert is_modal(TTSBackend.QWEN3_TTS) is True
    assert is_modal(TTSBackend.ZONOS2) is True
    assert is_modal(TTSBackend.VOXCPM2) is True
    assert is_modal(TTSBackend.EDGE) is False
    assert is_modal(TTSBackend.OMNIVOICE) is False
    assert is_modal(TTSBackend.FISH) is False
    assert is_modal(TTSBackend.OPENAI) is False


# --- gateway contract --------------------------------------------------------


def test_gateway_headers_without_a_key_send_no_credential() -> None:
    """An empty key must not emit an empty header."""
    assert gateway_headers() == {"Accept": "audio/wav, application/json"}
    assert "X-API-Key" not in gateway_headers("")


def test_gateway_headers_include_the_key_when_configured() -> None:
    """A configured key travels as X-API-Key."""
    assert gateway_headers("secret")["X-API-Key"] == "secret"


def test_parse_gateway_error_reads_the_nested_body() -> None:
    """The gateway nests its detail; both levels must be understood."""
    response = httpx.Response(
        404,
        json={"detail": {"detail": "voice_not_found", "error_type": "voice_not_found"}},
    )
    error = parse_gateway_error(response)

    assert error.status_code == 404
    assert error.error_type == "voice_not_found"
    assert str(error) == "voice_not_found"


def test_parse_gateway_error_lists_suggested_voice_ids() -> None:
    """A rejection that offers alternatives puts them in the message."""
    response = httpx.Response(
        404,
        json={
            "detail": {
                "detail": "nope",
                "error_type": "voice_not_found",
                "valid_voice_ids": ["es-mx-male", "es-mx-female"],
            }
        },
    )
    error = parse_gateway_error(response)

    assert error.valid_voice_ids == ("es-mx-male", "es-mx-female")
    assert "es-mx-male" in str(error)


def test_parse_gateway_error_tolerates_a_flat_detail() -> None:
    """A plain string detail still yields a readable message."""
    response = httpx.Response(400, json={"detail": "unsupported_language"})

    assert str(parse_gateway_error(response)) == "unsupported_language"


def test_parse_gateway_error_tolerates_a_non_json_body() -> None:
    """An HTML error page must not crash the caller."""
    response = httpx.Response(502, text="<html>bad gateway</html>")

    error = parse_gateway_error(response)
    assert error.status_code == 502
    assert "502" in str(error)


# --- gateway voice defaults --------------------------------------------------


def test_gateway_voice_for_language_covers_the_documented_defaults() -> None:
    """es and en have known stock ids."""
    assert gateway_voice_for_language("es") == "es-mx-male"
    assert gateway_voice_for_language("EN") == "en-us-male"


def test_gateway_voice_for_language_refuses_to_guess() -> None:
    """An unknown language asks for an explicit voice instead of inventing one."""
    with pytest.raises(TTSBackendError, match="no default tts-api voice"):
        gateway_voice_for_language("pt")


# --- voice_for ---------------------------------------------------------------


async def test_voice_for_edge_resolves_against_the_edge_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy alias maps to the neural ShortName for edge."""
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [])

    assert await voice_for(TTSBackend.EDGE, "es-mx-male", "es") == "es-MX-JorgeNeural"


async def test_voice_for_gateway_maps_an_edge_shortname_to_the_stock_id() -> None:
    """Switching engines keeps the built-in voices instead of dropping them."""
    resolved = await voice_for(TTSBackend.OMNIVOICE, "es-MX-JorgeNeural", "es")

    assert resolved == "es-mx-male"


async def test_voice_for_gateway_passes_a_stock_id_through() -> None:
    """A gateway id configured directly is not rewritten."""
    assert await voice_for(TTSBackend.OMNIVOICE, "es-mx-female", "es") == "es-mx-female"


async def test_voice_for_gateway_defaults_by_language() -> None:
    """No voice configured means the language's stock default."""
    assert await voice_for(TTSBackend.OMNIVOICE, "", "en") == "en-us-male"


async def test_voice_for_gateway_rejects_an_unmapped_language() -> None:
    """A language with no known stock id fails with instructions."""
    with pytest.raises(TTSBackendError, match="no default tts-api voice"):
        await voice_for(TTSBackend.OMNIVOICE, "", "pt")


async def test_voice_for_modal_picks_the_language_default(monkeypatch) -> None:
    """An unset voice resolves to the catalog's first enabled profile."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    resolved = await voice_for(TTSBackend.QWEN3_TTS, "", "es")

    assert resolved.startswith("es_")


async def test_voice_for_modal_accepts_a_catalog_name(monkeypatch) -> None:
    """A configured catalog profile is used verbatim."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    assert await voice_for(TTSBackend.QWEN3_TTS, "es_anchor_a", "es") == "es_anchor_a"


async def test_voice_for_modal_ignores_a_leftover_edge_voice(monkeypatch) -> None:
    """The global TTS_VOICE default must not fail a Modal collection."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    resolved = await voice_for(TTSBackend.ZONOS2, "es-MX-JorgeNeural", "es")

    assert resolved == "es_anchor_a"


async def test_voice_for_modal_rejects_a_real_typo(monkeypatch) -> None:
    """A name that is not in any engine's vocabulary is still an error."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    with pytest.raises(TTSBackendError, match="not in the voice catalog"):
        await voice_for(TTSBackend.QWEN3_TTS, "no_such_voice", "es")


async def test_voice_for_modal_fails_when_the_language_has_no_profile(
    monkeypatch,
) -> None:
    """A catalog without the requested language must not silently reuse another."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    with pytest.raises(TTSBackendError, match="no enabled voice profile"):
        await voice_for(TTSBackend.QWEN3_TTS, "", "fr")


async def test_voice_for_local_qwen_maps_the_profile_to_a_gateway_clone(
    monkeypatch,
) -> None:
    """run_in_modal=False maps the catalog profile to its gateway clone UUID."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    async def fake_resolve(voice: str, language: str) -> str:
        assert voice == "es_anchor_a"
        assert language == "es"
        return "uuid-es-anchor"

    monkeypatch.setattr("src.api.services.gateway_voices.resolve_gateway_qwen_voice", fake_resolve)

    resolved = await voice_for(TTSBackend.QWEN3_TTS, "es_anchor_a", "es", run_in_modal=False)

    assert resolved == "uuid-es-anchor"


async def test_voice_for_local_qwen_passes_a_clone_name_to_the_gateway(
    monkeypatch,
) -> None:
    """A gateway clone name skips the Modal catalog and resolves to its UUID."""
    seen: dict[str, str] = {}

    async def fake_resolve(voice: str, language: str) -> str:
        seen["voice"] = voice
        return "uuid-es-juan"

    monkeypatch.setattr("src.api.services.gateway_voices.resolve_gateway_qwen_voice", fake_resolve)

    resolved = await voice_for(
        TTSBackend.QWEN3_TTS, "es_ma_juan__qwen3_tts", "es", run_in_modal=False
    )

    assert seen["voice"] == "es_ma_juan__qwen3_tts"
    assert resolved == "uuid-es-juan"


async def test_voice_for_local_qwen_defaults_by_language_before_resolving(
    monkeypatch,
) -> None:
    """An unset voice picks the language's profile first, then clones it."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)
    seen: dict[str, str] = {}

    async def fake_resolve(voice: str, language: str) -> str:
        seen["voice"] = voice
        return "uuid-default"

    monkeypatch.setattr("src.api.services.gateway_voices.resolve_gateway_qwen_voice", fake_resolve)

    resolved = await voice_for(TTSBackend.QWEN3_TTS, "", "es", run_in_modal=False)

    assert seen["voice"].startswith("es_")
    assert resolved == "uuid-default"


async def test_voice_for_modal_path_never_touches_the_clone_map(monkeypatch) -> None:
    """The default (and run_in_modal=True) keep the catalog profile verbatim."""
    monkeypatch.setattr("src.core.voice_catalog.load_profiles", _profiles)

    async def boom(voice: str, language: str) -> str:
        raise AssertionError("the clone map belongs to the local gateway path only")

    monkeypatch.setattr("src.api.services.gateway_voices.resolve_gateway_qwen_voice", boom)

    assert await voice_for(TTSBackend.QWEN3_TTS, "es_anchor_a", "es") == "es_anchor_a"
    assert (
        await voice_for(TTSBackend.QWEN3_TTS, "es_anchor_a", "es", run_in_modal=True)
        == "es_anchor_a"
    )


# --- synthesize dispatch -----------------------------------------------------


async def test_synthesize_edge_calls_the_local_engine(monkeypatch) -> None:
    """The edge branch reaches synthesize_mp3 with the resolved voice."""
    seen: dict[str, object] = {}

    async def fake_mp3(text: str, voice: str, *, timeout: int | None = None) -> bytes:
        seen.update(text=text, voice=voice, timeout=timeout)
        return b"mp3-bytes"

    monkeypatch.setattr(tts_module, "synthesize_mp3", fake_mp3)

    audio = await synthesize(TTSBackend.EDGE, "hola", "es-MX-JorgeNeural", "es")

    assert audio == b"mp3-bytes"
    assert seen["voice"] == "es-MX-JorgeNeural"


async def test_voice_for_openai_defaults_to_its_stock_voice(monkeypatch) -> None:
    """An empty/Edge voice picks the provider's stock id for the language."""
    from src.api.services.voices import VoicesService

    async def fake_stock(self, language=None, force=False):
        return [
            {"voice_id": "es-mx-male", "language": "es", "engine": "omni_voice"},
            {"voice_id": "es_openai", "language": "es", "engine": "openai_tts"},
        ]

    monkeypatch.setattr(VoicesService, "list_stock", fake_stock)

    assert await voice_for(TTSBackend.OPENAI, "", "es") == "es_openai"
    assert await voice_for(TTSBackend.OPENAI, "es-MX-JorgeNeural", "es") == "es_openai"


async def test_voice_for_fish_resolves_catalog_profiles_to_clones(monkeypatch) -> None:
    """Fish catalog profiles go through the inline-clone resolver."""
    seen: dict[str, str] = {}

    async def fake_fish(voice: str, language: str) -> str:
        seen.update(voice=voice, language=language)
        return "fish-clone-uuid"

    monkeypatch.setattr("src.api.services.gateway_voices.resolve_gateway_fish_voice", fake_fish)

    assert await voice_for(TTSBackend.FISH, "marie_fr", "fr") == "fish-clone-uuid"
    assert seen == {"voice": "marie_fr", "language": "fr"}


async def test_synthesize_gateway_calls_the_gateway(monkeypatch) -> None:
    """The gateway branch posts to tts-api and returns its WAV."""
    seen: dict[str, object] = {}

    async def fake_gateway(text, voice_id, language, **kwargs):
        seen.update(text=text, voice_id=voice_id, language=language)
        return b"RIFFwav"

    monkeypatch.setattr(tts_backends, "gateway_tts", fake_gateway)

    audio = await synthesize(TTSBackend.OMNIVOICE, "hola", "es-mx-male", "es")

    assert audio == b"RIFFwav"
    assert seen["voice_id"] == "es-mx-male"


async def test_synthesize_cloud_engines_pin_the_gateway_engine(monkeypatch) -> None:
    """Fish/OpenAI never take the Modal branch and always pin their engine."""
    seen: list[dict[str, object]] = []

    async def fake_gateway(text, voice_id, language, **kwargs):
        seen.append({"voice_id": voice_id, "engine": kwargs.get("engine")})
        return b"RIFFwav"

    monkeypatch.setattr(tts_backends, "gateway_tts", fake_gateway)

    assert await synthesize(TTSBackend.FISH, "hola", "es_fish", "es") == b"RIFFwav"
    assert await synthesize(TTSBackend.OPENAI, "hola", "es_openai", "es") == b"RIFFwav"
    assert seen == [
        {"voice_id": "es_fish", "engine": "fish_tts"},
        {"voice_id": "es_openai", "engine": "openai_tts"},
    ]


async def test_synthesize_modal_requires_the_kill_switch(monkeypatch) -> None:
    """Modal off is reported before any network call is attempted."""
    monkeypatch.setattr(tts_backends.get_settings(), "modal_enabled", False)

    with pytest.raises(TTSBackendError, match="modal is disabled"):
        await synthesize(TTSBackend.QWEN3_TTS, "hola", "voice", "es")


async def test_synthesize_local_qwen_pins_the_gateway_engine(monkeypatch) -> None:
    """run_in_modal=False posts to the gateway with engine=qwen3_tts (no Modal)."""
    seen: dict[str, object] = {}

    async def fake_gateway(text, voice_id, language, **kwargs):
        seen.update(voice_id=voice_id, language=language, engine=kwargs.get("engine"))
        return b"RIFFwav"

    monkeypatch.setattr(tts_backends, "gateway_tts", fake_gateway)

    audio = await synthesize(TTSBackend.QWEN3_TTS, "hola", "uuid-es", "es", run_in_modal=False)

    assert audio == b"RIFFwav"
    assert seen["voice_id"] == "uuid-es"
    assert seen["engine"] == "qwen3_tts"


async def test_synthesize_qwen_explicit_modal_still_hits_the_guards(monkeypatch) -> None:
    """run_in_modal=True keeps the Modal spend guards, like the default."""
    monkeypatch.setattr(tts_backends.get_settings(), "modal_enabled", False)

    with pytest.raises(TTSBackendError, match="modal is disabled"):
        await synthesize(TTSBackend.QWEN3_TTS, "hola", "voice", "es", run_in_modal=True)


async def test_synthesize_voxcpm2_requires_the_gate(monkeypatch) -> None:
    """VoxCPM2 stays inert until its gate is flipped, like ZONOS2."""
    monkeypatch.setattr(tts_backends.get_settings(), "modal_enabled", True)
    monkeypatch.setattr(tts_backends.get_settings(), "voxcpm2_enabled", False)

    with pytest.raises(TTSBackendError, match="voxcpm2 is behind the benchmark gate"):
        await synthesize(TTSBackend.VOXCPM2, "hola", "voice", "es")


async def test_gateway_tts_posts_the_documented_payload() -> None:
    """The gateway contract is asserted against a real MockTransport."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, content=b"RIFF")

    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1",
        transport=httpx.MockTransport(handler),
        headers=gateway_headers(""),
    )
    audio = await tts_backends.gateway_tts("hola", "es-mx-male", "es", client=client)
    await client.aclose()

    assert audio == b"RIFF"
    assert captured["url"] == "http://gateway.test/api/v1/tts"
    body = json.loads(captured["body"])
    assert body["voice_id"] == "es-mx-male"
    assert body["language"] == "es"
    assert body["text"] == "hola"


async def test_gateway_instruct_posts_the_design_payload() -> None:
    """Voice design through the gateway pins qwen3_tts and sends the instruct."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"RIFF")

    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1",
        transport=httpx.MockTransport(handler),
        headers=gateway_headers(""),
    )
    audio = await tts_backends.gateway_instruct("Hello", "warm baritone", "en", client=client)
    await client.aclose()

    assert audio == b"RIFF"
    assert captured["url"] == "http://gateway.test/api/v1/tts/instruct"
    assert captured["body"] == {
        "text": "Hello",
        "instruct": "warm baritone",
        "language": "en",
        "engine": "qwen3_tts",
    }


async def test_gateway_tts_posts_speed_and_emotion() -> None:
    """Audiobook delivery profiles reach the gateway as request fields."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, content=b"RIFF")

    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1",
        transport=httpx.MockTransport(handler),
        headers=gateway_headers(""),
    )
    await tts_backends.gateway_tts(
        "hola", "es-mx-male", "es", client=client, speed=0.9, emotion="calm"
    )
    await client.aclose()

    body = json.loads(captured["body"])
    assert body["speed"] == 0.9
    assert body["emotion"] == "calm"


async def test_synthesize_gateway_forwards_prosody(monkeypatch) -> None:
    """OmniVoice gets speed and emotion; Kokoro gets speed but no emotion."""
    seen: list[dict[str, object]] = []

    async def fake_gateway(text, voice_id, language, **kwargs):
        seen.append(kwargs)
        return b"RIFFwav"

    monkeypatch.setattr(tts_backends, "gateway_tts", fake_gateway)
    await synthesize(TTSBackend.OMNIVOICE, "hola", "es-mx-male", "es", speed=0.9, emotion="calm")
    await synthesize(TTSBackend.KOKORO, "hola", "es-mx-male", "es", speed=0.8, emotion="calm")

    assert seen[0]["speed"] == 0.9
    assert seen[0]["emotion"] == "calm"
    assert seen[1]["speed"] == 0.8
    assert seen[1]["emotion"] is None


async def test_gateway_tts_raises_a_typed_error_on_failure() -> None:
    """A 4xx from the gateway is a TTSGatewayError, not a bare status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"detail": "gone"}})

    client = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(TTSGatewayError) as excinfo:
        await tts_backends.gateway_tts("hola", "es-mx-male", "es", client=client)
    await client.aclose()

    assert excinfo.value.status_code == 404
    assert str(excinfo.value) == "gone"


# --- service integration: cache key and extension ----------------------------


async def test_switching_backend_regenerates_and_changes_the_extension(
    tmp_path: Path, monkeypatch
) -> None:
    """The engine is part of the cache key, so edge bytes are never served as WAV."""
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [{"ShortName": "es-MX-JorgeNeural"}])
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    base = {"markdown": "hola mundo", "voice": "es-MX-JorgeNeural", "language": "es"}

    with_edge = await service.generate_tts(TTSIn(**base, collection="tech"))

    assert with_edge.backend == "edge"
    assert with_edge.audio_path.endswith(".mp3")

    async def fake_gateway(text, voice_id, language, **kwargs):
        return b"RIFFwav-bytes"

    monkeypatch.setattr(tts_backends, "gateway_tts", fake_gateway)
    with_gateway = await service.generate_tts(
        TTSIn(**base, collection="tech", backend="omni_voice")
    )

    assert with_gateway.backend == "omni_voice"
    assert with_gateway.audio_path.endswith(".wav")
    assert with_gateway.cached is False
    assert with_gateway.audio_path != with_edge.audio_path
    assert Path(with_gateway.audio_path).read_bytes() == b"RIFFwav-bytes"


async def test_cache_probe_reports_a_hit_without_synthesizing(tmp_path: Path, monkeypatch) -> None:
    """The Modal budget guard needs to know a render is free before spending."""
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [{"ShortName": "es-MX-JorgeNeural"}])
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(markdown="hola", voice="es-MX-JorgeNeural", language="es", collection="x")

    assert await service.cache_probe(payload) is False
    await service.generate_tts(payload)
    assert await service.cache_probe(payload) is True


async def test_fallback_audio_is_not_pinned_as_a_cache_hit(tmp_path: Path, monkeypatch) -> None:
    """Degrading to edge must not make the next run skip the requested engine."""
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [{"ShortName": "es-MX-JorgeNeural"}])
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    monkeypatch.setattr(tts_backends.get_settings(), "tts_fallback_to_edge", True)

    calls = {"gateway": 0}

    async def failing_gateway(text, voice_id, language, **kwargs):
        calls["gateway"] += 1
        raise TTSGatewayError("gateway is down", status_code=503)

    monkeypatch.setattr(tts_backends, "gateway_tts", failing_gateway)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(
        markdown="hola", voice="es-mx-male", language="es", collection="y", backend="omni_voice"
    )

    first = await service.generate_tts(payload)
    second = await service.generate_tts(payload)

    assert first.backend == "edge"
    assert calls["gateway"] == 2, "the requested engine must be retried every run"
    assert second.cached is False


async def test_fallback_is_off_by_default(tmp_path: Path, monkeypatch) -> None:
    """A failing engine reports the failure instead of silently swapping voices."""
    monkeypatch.setattr(tts_backends.get_settings(), "tts_fallback_to_edge", False)

    async def failing_gateway(text, voice_id, language, **kwargs):
        raise TTSGatewayError("gateway is down", status_code=503)

    monkeypatch.setattr(tts_backends, "gateway_tts", failing_gateway)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(
        markdown="hola", voice="es-mx-male", language="es", collection="z", backend="omni_voice"
    )

    with pytest.raises(TTSGatewayError, match="gateway is down"):
        await service.generate_tts(payload)


# --- helpers -----------------------------------------------------------------


class _FakeCommunicate:
    """Minimal stand-in for edge_tts.Communicate."""

    def __init__(self, text: str, voice: str, **kwargs) -> None:
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": b"mp3-bytes"}


class _Profile:
    """Duck-typed voice profile for the catalog-dependent tests."""

    def __init__(self, name: str, language: str, enabled: bool = True) -> None:
        self.name = name
        self.language = language
        self.enabled = enabled


def _profiles() -> list[_Profile]:
    """Return a two-language catalog that does not depend on the repo file."""
    return [
        _Profile("en_anchor_a", "en"),
        _Profile("es_anchor_a", "es"),
        _Profile("es_anchor_b", "es", enabled=False),
    ]
