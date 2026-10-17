"""Unit tests for the Phase 7.11 HMAC-signed audio links."""

from __future__ import annotations

import time

import pytest

from src.api.services.signed_links import (
    SignedLinkError,
    build_signed_path,
    verify_signed_link,
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable signed links with a known secret for every test."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_link_secret", "unit-test-secret")


def test_build_and_verify_round_trip() -> None:
    """A freshly built link verifies successfully."""
    path = build_signed_path("doc-tts-abc123", ttl=600)

    assert path.startswith("/api/audio/doc/doc-tts-abc123?exp=")
    query = path.split("?", 1)[1]
    exp = query.split("&", 1)[0].split("=", 1)[1]
    sig = query.split("sig=", 1)[1]
    verify_signed_link("doc-tts-abc123", exp, sig)  # no exception


def test_build_rejects_a_disabled_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a configured secret there is no link to hand out."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_link_secret", "")
    with pytest.raises(SignedLinkError, match="not configured"):
        build_signed_path("doc-tts-abc123")


def test_verify_rejects_an_expired_link() -> None:
    """A link past its expiry is refused."""
    past = int(time.time()) - 10
    path = build_signed_path("doc-tts-abc123", ttl=-5)
    sig = path.split("sig=", 1)[1]

    with pytest.raises(SignedLinkError, match="expired"):
        verify_signed_link("doc-tts-abc123", str(past), sig)


def test_verify_rejects_a_tampered_expiry() -> None:
    """Extending the expiry without the secret breaks the signature."""
    path = build_signed_path("doc-tts-abc123", ttl=60)
    exp = path.split("exp=", 1)[1].split("&", 1)[0]
    sig = path.split("sig=", 1)[1]

    with pytest.raises(SignedLinkError, match="signature mismatch"):
        verify_signed_link("doc-tts-abc123", str(int(exp) + 86_400), sig)


def test_verify_rejects_a_tampered_signature() -> None:
    """A flipped signature is refused."""
    path = build_signed_path("doc-tts-abc123", ttl=60)
    exp = path.split("exp=", 1)[1].split("&", 1)[0]
    sig = path.split("sig=", 1)[1]

    with pytest.raises(SignedLinkError, match="signature mismatch"):
        verify_signed_link("doc-tts-abc123", exp, sig[:-2] + ("00" if sig[-2:] != "00" else "01"))


def test_verify_rejects_a_foreign_workflow_id() -> None:
    """A valid signature over another id cannot be replayed here."""
    path = build_signed_path("doc-tts-abc123", ttl=60)
    exp = path.split("exp=", 1)[1].split("&", 1)[0]
    sig = path.split("sig=", 1)[1]

    with pytest.raises(SignedLinkError, match="signature mismatch"):
        verify_signed_link("doc-tts-other456", exp, sig)


@pytest.mark.parametrize("workflow_id", ["", "../etc", "a/b", "con espacios"])
def test_build_rejects_foreign_ids(workflow_id: str) -> None:
    """Ids outside our alphabet never make it into a link."""
    with pytest.raises(SignedLinkError, match="invalid workflow id"):
        build_signed_path(workflow_id)


def test_verify_rejects_malformed_expiry() -> None:
    """A non-numeric expiry is a malformed link, not a crash."""
    with pytest.raises(SignedLinkError, match="malformed expiry"):
        verify_signed_link("doc-tts-abc123", "soon", "deadbeef")


def test_verify_rejects_an_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the secret also closes verification (fail closed)."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_link_secret", "")
    with pytest.raises(SignedLinkError, match="not configured"):
        verify_signed_link("doc-tts-abc123", "9999999999", "00")
