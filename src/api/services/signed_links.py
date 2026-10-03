"""HMAC-signed links for document audio (Phase 7.11).

A document's audio is not delivered to a channel: it is offered as a URL the
operator can open from the LAN or over Tailscale. The link itself carries the
authorization (``exp`` + ``sig``), so it can be pasted into a phone browser
without the admin token, while still expiring and being unforgeable.
"""

from __future__ import annotations

import hmac
import re
import time
from hashlib import sha256

from src.core.config import get_settings

# Workflow ids we generate ("doc-tts-<hex>"). Anything else never reaches the
# filesystem: the id only selects a DB row, but validating it keeps query
# strings from smuggling path separators into lookups.
_WORKFLOW_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")

AUDIO_LINK_PREFIX = "/api/audio/doc"


class SignedLinkError(Exception):
    """The link is disabled, malformed, expired or forged."""


def _validate_workflow_id(workflow_id: str) -> str:
    """Reject ids that are not one of ours.

    Args:
        workflow_id: Workflow id embedded in the link.

    Returns:
        The validated id.

    Raises:
        SignedLinkError: When the id is empty or contains foreign characters.
    """
    if not workflow_id or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise SignedLinkError("invalid workflow id")
    return workflow_id


def _mac(secret: str, workflow_id: str, exp: int) -> str:
    """Return the hex HMAC-SHA256 over '<workflow_id>:<exp>'.

    Args:
        secret: Shared secret from ``doc_tts_link_secret``.
        workflow_id: Workflow id the link points at.
        exp: Expiry unix timestamp.

    Returns:
        Hex digest.
    """
    message = f"{workflow_id}:{exp}".encode()
    return hmac.new(secret.encode(), message, sha256).hexdigest()


def build_signed_path(workflow_id: str, *, ttl: int | None = None, now: int | None = None) -> str:
    """Build a ready-to-open signed audio path.

    Args:
        workflow_id: Workflow id of the finished document job.
        ttl: Seconds the link stays valid. Defaults to
            ``doc_tts_link_ttl_seconds``.
        now: Unix timestamp to anchor expiry on. Defaults to the current time.

    Returns:
        Path like ``/api/audio/doc/doc-tts-ab12?exp=1760000000&sig=...``.

    Raises:
        SignedLinkError: When signed links are disabled (empty secret) or the
            id is not valid.
    """
    settings = get_settings()
    secret = settings.doc_tts_link_secret.strip()
    if not secret:
        raise SignedLinkError("doc_tts_link_secret is not configured")
    cleaned = _validate_workflow_id(workflow_id)
    expires = int(now if now is not None else time.time()) + int(
        ttl if ttl is not None else settings.doc_tts_link_ttl_seconds
    )
    sig = _mac(secret, cleaned, expires)
    return f"{AUDIO_LINK_PREFIX}/{cleaned}?exp={expires}&sig={sig}"


def verify_signed_link(workflow_id: str, exp: str | int, sig: str) -> None:
    """Verify a signed audio link, raising when it must be refused.

    Args:
        workflow_id: Workflow id from the path.
        exp: Expiry unix timestamp from the query string.
        sig: Signature hex digest from the query string.

    Raises:
        SignedLinkError: When links are disabled, the id is foreign, the
            expiry is malformed or in the past, or the signature does not
            match (forged or tampered).
    """
    settings = get_settings()
    secret = settings.doc_tts_link_secret.strip()
    if not secret:
        raise SignedLinkError("doc_tts_link_secret is not configured")
    cleaned = _validate_workflow_id(workflow_id)
    try:
        expires = int(exp)
    except (TypeError, ValueError) as exc:
        raise SignedLinkError("malformed expiry") from exc
    if expires < int(time.time()):
        raise SignedLinkError("link expired")
    if not isinstance(sig, str) or not sig:
        raise SignedLinkError("missing signature")
    if not hmac.compare_digest(sig, _mac(secret, cleaned, expires)):
        raise SignedLinkError("signature mismatch")
