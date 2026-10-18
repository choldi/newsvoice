"""In-process stand-in for the gateway's voice-catalog API (P6).

news-engine reads the voice catalog from the gateway since P6, so any test
that renders a dropdown, queues a design run or previews a reference needs a
gateway to answer. This fake speaks the three catalog endpoints over
``httpx.MockTransport`` and keeps its state in memory:

- it serves the committed catalog that ships with ``apps/gateway`` (the very
  file the gateway serves, so the profiles a test sees are the real ones);
- it accepts the custom profiles and the reference WAVs news-engine uploads,
  enforcing the rules the gateway enforces (409 on a taken name, 404 on an
  unknown profile, 422 on unusable audio);
- it can refuse every call, which is how the tests pin the behaviour of an
  unreachable gateway.

Install it with :func:`install_fake_gateway`, which the shared conftest does
for every test.
"""

from __future__ import annotations

import email
import json
from email import policy
from pathlib import Path

import httpx
import pytest
from tts_contract import (
    VoiceCatalogError,
    VoiceCatalogResponse,
    VoiceManifestEntry,
    VoiceProfile,
    classify_status,
    is_voice_slug,
    load_catalog_profiles,
    missing_or_stale,
    sha256_bytes,
    sha256_text,
)

from src.core.audio import CANONICAL_FORMAT, AudioError, build_wav, wav_duration, wav_format

#: The committed catalog the gateway ships with, which the fake serves as-is.
COMMITTED_CATALOG = Path(__file__).resolve().parents[2] / "gateway" / "voice_profiles.json"

#: Base URL the injected clients point at; nothing ever resolves it.
BASE_URL = "http://gateway.test/api/v1"


class FakeGateway:
    """The catalog endpoints of a gateway, in memory.

    Args:
        committed: Committed catalog JSON to serve. Defaults to the file the
            gateway ships with.
    """

    def __init__(self, committed: Path = COMMITTED_CATALOG) -> None:
        """Load the committed catalog and start with an empty runtime half."""
        self.committed = Path(committed)
        self.custom: list[VoiceProfile] = []
        self.manifest: dict[str, VoiceManifestEntry] = {}
        self.refs: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self._unavailable: int | None = None
        self.http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.handle))

    @property
    def unavailable(self) -> int | None:
        """Status the fake answers instead of the catalog, or None when healthy."""
        return self._unavailable

    @unavailable.setter
    def unavailable(self, status: int | None) -> None:
        """Refuse every call and drop the snapshot, as a real outage would."""
        self._unavailable = status
        invalidate_client()

    # --- seeding ---------------------------------------------------------

    def add_custom(self, profile: VoiceProfile) -> VoiceProfile:
        """Add an operator-created profile, as ``POST /voices/catalog/custom`` does.

        Args:
            profile: Profile to append.

        Returns:
            The stored profile.

        Raises:
            VoiceCatalogError: When the name is already taken.
        """
        return self._save_custom(profile)

    def add_reference(self, name: str, wav: bytes, *, instruct: str, ref_text: str) -> str:
        """Store a reference WAV and its manifest entry, as the POST endpoint does.

        Args:
            name: Profile name (slug, no suffix).
            wav: Raw WAV bytes.
            instruct: Profile instruct at generation time.
            ref_text: Profile transcript at generation time.

        Returns:
            The manifest entry file name.

        Raises:
            VoiceCatalogError: When the name or the audio is unusable.
        """
        entry = self._store_reference(name, wav, instruct=instruct, ref_text=ref_text)
        invalidate_client()
        return entry.file

    # --- answers ---------------------------------------------------------

    def payload(self) -> VoiceCatalogResponse:
        """Build the ``GET /voices/catalog`` body.

        Returns:
            Profiles, manifest, per-profile status and the name lists.

        Raises:
            VoiceCatalogError: When a name is defined twice.
        """
        committed = load_catalog_profiles(self.committed)
        profiles = [*committed, *self.custom]
        known = {profile.name for profile in committed}
        clashes = sorted(known & {profile.name for profile in self.custom})
        if clashes:
            raise VoiceCatalogError(f"duplicate voice profile names: {clashes}")
        manifest = dict(self.manifest)
        pending = set(missing_or_stale(profiles, manifest))
        return VoiceCatalogResponse(
            profiles=profiles,
            generated=sorted(
                profile.name
                for profile in profiles
                if profile.name not in pending and profile.enabled
            ),
            missing=sorted(pending),
            stale=sorted(pending),
            custom=sorted(profile.name for profile in self.custom),
            statuses={
                profile.name: classify_status(
                    profile,
                    manifest,
                    has_audio=profile.name in self.refs,
                )
                for profile in profiles
            },
            manifest=manifest,
        )

    def has_reference(self, name: str) -> bool:
        """Report whether a reference WAV is stored for a profile.

        Args:
            name: Profile name, optionally with a ``.wav`` suffix.

        Returns:
            True when the fake holds bytes for that profile.
        """
        return _stem(name) in self.refs

    # --- transport -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one catalog request.

        Args:
            request: Request as httpx built it.

        Returns:
            The response news-engine would get from a real gateway.
        """
        self.requests.append(request)
        if self._unavailable is not None:
            return self._answer(self._unavailable, {"detail": "gateway unavailable"})
        path = request.url.path
        if request.method == "GET" and path.endswith("/voices/catalog"):
            try:
                body = self.payload()
            except VoiceCatalogError as exc:
                return self._answer(500, {"detail": str(exc)})
            return self._answer(200, body.model_dump(mode="json"))
        if path.endswith("/voices/catalog/custom") and request.method == "POST":
            return self._create_custom(request)
        if "/voices/catalog/refs/" in path and request.method == "POST":
            return self._store(request)
        if "/voices/catalog/refs/" in path and request.method == "GET":
            return self._serve_reference(path)
        return self._answer(404, {"detail": f"no such endpoint: {path}"})

    # --- endpoints -------------------------------------------------------

    def _create_custom(self, request: httpx.Request) -> httpx.Response:
        """Answer ``POST /voices/catalog/custom``.

        Args:
            request: The incoming request.

        Returns:
            201 with the saved profile, or the refusal the gateway gives.
        """
        try:
            body = json.loads(request.content)
        except ValueError:
            return self._answer(422, {"detail": "custom voice profile is not JSON"})
        try:
            profile = VoiceProfile.model_validate(body)
        except ValueError as exc:
            return self._answer(422, {"detail": str(exc)})
        try:
            saved = self._save_custom(profile)
        except VoiceCatalogError as exc:
            return self._answer(409, {"detail": str(exc)})
        return self._answer(201, saved.model_dump(mode="json"))

    def _save_custom(self, profile: VoiceProfile) -> VoiceProfile:
        """Append a custom profile under the gateway's clash rules.

        Args:
            profile: Profile to append.

        Returns:
            The stored profile.

        Raises:
            VoiceCatalogError: When the name is already taken.
        """
        if any(saved.name == profile.name for saved in self.custom):
            raise VoiceCatalogError(f"custom voice profile already exists: {profile.name!r}")
        committed = {saved.name for saved in load_catalog_profiles(self.committed)}
        if profile.name in committed:
            raise VoiceCatalogError(f"voice profile name already in the catalog: {profile.name!r}")
        self.custom.append(profile)
        return profile

    def _store(self, request: httpx.Request) -> httpx.Response:
        """Answer ``POST /voices/catalog/refs/{name}``.

        Args:
            request: The incoming multipart request.

        Returns:
            201 with the manifest entry, or the refusal the gateway gives.
        """
        name = _stem(request.url.path.rsplit("/", 1)[-1])
        fields = _multipart(request)
        wav = fields.get("file", b"")
        instruct = fields.get("instruct", b"").decode("utf-8", "replace")
        ref_text = fields.get("ref_text", b"").decode("utf-8", "replace")
        design_model = fields.get("design_model", b"").decode("utf-8", "replace")
        try:
            entry = self._store_reference(
                name,
                wav,
                instruct=instruct,
                ref_text=ref_text,
                design_model=design_model,
            )
        except VoiceCatalogError as exc:
            code = 404 if "unknown" in str(exc) else 422
            return self._answer(code, {"detail": str(exc)})
        return self._answer(201, entry.model_dump(mode="json"))

    def _store_reference(
        self,
        name: str,
        wav: bytes,
        *,
        instruct: str,
        ref_text: str,
        design_model: str = "",
    ) -> VoiceManifestEntry:
        """Validate and store a reference, as the gateway service does.

        Args:
            name: Profile name (slug, no suffix).
            wav: Raw WAV bytes.
            instruct: Profile instruct at generation time.
            ref_text: Profile transcript at generation time.
            design_model: Model that produced the reference.

        Returns:
            The manifest entry that now describes the reference.

        Raises:
            VoiceCatalogError: When the name, the audio or the profile is
                unusable.
        """
        if not is_voice_slug(name):
            raise VoiceCatalogError(f"invalid voice name: {name!r}")
        if not wav:
            raise VoiceCatalogError(f"empty reference audio for {name!r}")
        known = {profile.name for profile in self.payload().profiles}
        if name not in known:
            raise VoiceCatalogError(f"unknown voice profile: {name!r}")
        try:
            sample_rate = wav_format(wav).framerate
            duration = wav_duration(wav)
        except AudioError as exc:
            raise VoiceCatalogError(f"reference audio is not a readable WAV: {exc}") from exc
        if not instruct.strip() or not ref_text.strip():
            raise VoiceCatalogError(f"reference audio for {name!r} needs instruct and ref_text")
        entry = VoiceManifestEntry(
            file=f"refs/{name}.wav",
            sha256=sha256_bytes(wav),
            sample_rate=sample_rate,
            duration_seconds=duration,
            design_model=design_model,
            instruct_sha256=sha256_text(instruct),
            ref_text_sha256=sha256_text(ref_text),
            created_at="2026-10-18T00:00:00+00:00",
        )
        self.refs[name] = wav
        self.manifest[name] = entry
        return entry

    def _serve_reference(self, path: str) -> httpx.Response:
        """Answer ``GET /voices/catalog/refs/{name}``.

        Args:
            path: Request path carrying the profile name.

        Returns:
            The WAV bytes, or 404 when there is none.
        """
        name = _stem(path.rsplit("/", 1)[-1])
        if not is_voice_slug(name):
            return self._answer(404, {"detail": f"voice reference not found: {name!r}"})
        wav = self.refs.get(name)
        if wav is None:
            return self._answer(404, {"detail": f"voice reference not found: {name!r}"})
        return httpx.Response(200, content=wav, headers={"Content-Type": "audio/wav"})

    @staticmethod
    def _answer(status_code: int, body: dict[str, object]) -> httpx.Response:
        """Build a JSON response the dispatcher can parse.

        Args:
            status_code: HTTP status.
            body: JSON body.

        Returns:
            The response.
        """
        return httpx.Response(status_code, json=body)


_installed: FakeGateway | None = None


def current_gateway() -> FakeGateway:
    """Return the fake installed for the running test.

    Returns:
        The installed fake.

    Raises:
        RuntimeError: When no fake has been installed (the shared conftest
            installs one for every test).
    """
    if _installed is None:
        raise RuntimeError("no fake gateway installed; use the shared conftest fixture")
    return _installed


def save_custom_profile(profile: VoiceProfile) -> VoiceProfile:
    """Seed an operator profile into the installed fake gateway.

    The test seam for "this operator already created a voice": the same shape
    the catalog loader exposed before P6, so a test that only needs the profile
    to exist does not have to speak HTTP. Write-path tests use the real
    client instead (``voice_catalog_client.save_custom_profile``).

    Args:
        profile: Profile to append.

    Returns:
        The stored profile.

    Raises:
        VoiceCatalogError: When the name is already taken.
    """
    saved = current_gateway().add_custom(profile)
    invalidate_client()
    return saved


def sample_wav() -> bytes:
    """Build a short audible WAV, the payload the gateway accepts as a reference.

    Returns:
        WAV bytes in the canonical format.
    """
    return build_wav(b"\xff\x7f" * 2400, CANONICAL_FORMAT)


def invalidate_client() -> None:
    """Drop news-engine's cached catalog snapshot, as a write to the gateway does."""
    from src.api.services import voice_catalog_client as client

    client.invalidate_catalog()


def install_fake_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """Point the catalog client at a fresh fake gateway.

    Both halves of the client are redirected: the async dispatcher and the
    blocking read a cold synchronous reader performs, so no test ever opens a
    socket.

    Args:
        monkeypatch: ``pytest.MonkeyPatch`` used to redirect the client seams.

    Returns:
        The installed fake, ready to be seeded.
    """
    global _installed
    from src.api.services import voice_catalog_client as client

    gateway = FakeGateway()
    _installed = gateway
    monkeypatch.setattr(client, "_dispatcher", lambda: _dispatcher_for(gateway))
    monkeypatch.setattr(client, "_blocking_http", lambda: _blocking_http_for(gateway))
    client.invalidate_catalog()
    return gateway


def _dispatcher_for(gateway: FakeGateway) -> object:
    """Build the async dispatcher bound to a fake.

    Args:
        gateway: Fake answering the requests.

    Returns:
        A ``GatewayClient`` whose transport is the fake.
    """
    from src.api.services.gateway import GatewayClient

    return GatewayClient(client=gateway.http)


def _blocking_http_for(gateway: FakeGateway) -> httpx.Client:
    """Build the short-budget client bound to a fake.

    Args:
        gateway: Fake answering the requests.

    Returns:
        An ``httpx.Client`` whose transport is the fake.
    """
    return httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(gateway.handle))


def _stem(name: str) -> str:
    """Strip the optional ``.wav`` suffix of a reference path.

    Args:
        name: Profile name, with or without the suffix.

    Returns:
        The bare profile name.
    """
    return name[:-4] if name.lower().endswith(".wav") else name


def _multipart(request: httpx.Request) -> dict[str, bytes]:
    """Split a multipart body into its fields.

    Args:
        request: The incoming request.

    Returns:
        Field name to raw bytes, for both text fields and the file.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return {}
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    message = email.message_from_bytes(raw, policy=policy.default)
    fields: dict[str, bytes] = {}
    if not message.is_multipart():
        return fields
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        fields[str(name)] = part.get_payload(decode=True) or b""
    return fields


__all__ = [
    "BASE_URL",
    "COMMITTED_CATALOG",
    "FakeGateway",
    "current_gateway",
    "install_fake_gateway",
    "invalidate_client",
    "sample_wav",
    "save_custom_profile",
]
