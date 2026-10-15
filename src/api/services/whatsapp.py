"""WhatsApp delivery service for the daily news digest.

Sends the digest text and compressed M4A audio to one or more E.164 recipients
(and/or a group) through a self-hosted Baileys gateway container
(``blakegt/whatsapp-web-api-rest``): ``POST /message`` for text/media and
``POST /simulate`` for the composing typing indicator.

The service mirrors ``EmailService``: it is driven exclusively from the Temporal
activities and is fail-closed — without a configured API token the channel is
disabled, and any number not present in ``WHATSAPP_ALLOWLIST`` (groups excepted)
is rejected with a non-retryable error instead of being contacted.

To avoid WhatsApp anti-spam friction, each send is paced with an OpenClaw-style
human typing delay (``humanDelay``: off / natural / custom) plus a per-recipient
cool-down so the same contact is never pinged twice within the interval.
"""

import asyncio
import base64
import random
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from src.core.config import Settings, get_settings
from src.core.prompts import strip_emotion_tags
from src.core.tracing import trace_function

logger = structlog.get_logger()

# Error messages longer than this are hard for recipients to consume in one
# bubble; the digest is chunked before sending (see split_message_chunks).
WHATSAPP_MESSAGE_MAX_CHARS = 4000
# Uploads are base64-embedded in the JSON body; guard against multi-GB files.
WHATSAPP_AUDIO_MAX_BYTES = 16 * 1024 * 1024
# Natural human delay boundaries (seconds), OpenClaw default.
_NATURAL_DELAY_MIN = 0.8
_NATURAL_DELAY_MAX = 2.5
_GROUP_SUFFIX = "@g.us"
_PRIVATE_SUFFIX = "@c.us"


class WhatsAppError(Exception):
    """Base exception for the WhatsApp delivery service."""


class WhatsAppConfigError(WhatsAppError):
    """Raised when the Baileys gateway is not configured (fail-closed)."""


class WhatsAppNotAllowedError(WhatsAppError):
    """Raised when a recipient is not in the allowlist (fail-closed)."""


class WhatsAppDeliveryError(WhatsAppError):
    """Raised when the Baileys gateway rejects a send (network/API)."""


class WhatsAppService:
    """Send the digest text/audio to WhatsApp recipients via a Baileys gateway.

    Args:
        whatsapp_settings: Optional settings override. Defaults to the global
            ``get_settings()`` instance.
    """

    def __init__(self, whatsapp_settings: Settings | None = None) -> None:
        """Initialize with an optional settings override.

        Args:
            whatsapp_settings: Settings object (defaults to global settings).
        """
        self._cfg = whatsapp_settings or get_settings()
        # Per-recipient cool-down bookkeeping (JID -> monotonic timestamp).
        self._last_sent: dict[str, float] = {}

    @property
    def _configured(self) -> bool:
        """Return whether the Baileys gateway is configured.

        Returns:
            True when both whatsapp_api_token and whatsapp_api_url are set.
        """
        return bool(self._cfg.whatsapp_api_token and self._cfg.whatsapp_api_url)

    @property
    def _cooldown_seconds(self) -> float:
        """Return the per-recipient cool-down in seconds.

        Returns:
            whatsapp_recipient_cooldown_ms expressed in seconds.
        """
        return max(0.0, getattr(self._cfg, "whatsapp_recipient_cooldown_ms", 0) / 1000.0)

    @property
    def _typing_indicator(self) -> bool:
        """Return whether the composing typing indicator should be sent.

        Returns:
            True when whatsapp_typing_indicator equals 'composing'.
        """
        return getattr(self._cfg, "whatsapp_typing_indicator", "") == "composing"

    def _delay_seconds(self) -> float:
        """Return the human typing delay before a message, per delay mode.

        Returns:
            Seconds to sleep; 0 when delay mode is 'off'.
        """
        mode = getattr(self._cfg, "whatsapp_delay_mode", "natural")
        if mode == "off":
            return 0.0
        if mode == "custom":
            low = max(0, getattr(self._cfg, "whatsapp_delay_custom_min_ms", 2000))
            high = max(low, getattr(self._cfg, "whatsapp_delay_custom_max_ms", 8000))
            return random.uniform(low / 1000.0, high / 1000.0)
        return random.uniform(_NATURAL_DELAY_MIN, _NATURAL_DELAY_MAX)

    @staticmethod
    def _jid_for(number: str) -> str:
        """Convert an E.164 or already-suffixed JID into a private JID.

        Args:
            number: E.164 number or a JID with a known suffix.

        Returns:
            The JID used by the gateway: groups stay ``<id>@g.us`` and phone
            numbers become ``<digits>@c.us``.
        """
        if number.endswith(_GROUP_SUFFIX) or number.endswith(_PRIVATE_SUFFIX):
            return number
        digits = number.lstrip("+")
        return f"{digits}{_PRIVATE_SUFFIX}"

    @staticmethod
    def _jid_for_group(group_id: str) -> str:
        """Convert a WhatsApp group id into a group JID.

        Args:
            group_id: Group id (with or without the @g.us suffix).

        Returns:
            The JID used by the gateway.
        """
        gid = group_id.strip()
        return gid if gid.endswith(_GROUP_SUFFIX) else f"{gid}{_GROUP_SUFFIX}"

    def _is_allowed(self, number: str) -> bool:
        """Fail-closed allowlist check for a raw recipient.

        Args:
            number: Raw E.164 number or group id (pre-JID conversion).

        Returns:
            True for allowlisted numbers and any group; False otherwise.
        """
        if number.endswith(_GROUP_SUFFIX):
            return True
        allowlist = {entry.strip() for entry in (self._cfg.whatsapp_allowlist or [])}
        return number in allowlist

    @staticmethod
    def _normalize_recipients(raw: list[str]) -> list[str]:
        """Dedupe and strip whitespace from E.164 recipients preserving order.

        Args:
            raw: Raw recipient list.

        Returns:
            De-duplicated, non-empty numbers.
        """
        seen: set[str] = set()
        result: list[str] = []
        for number in raw:
            clean = number.strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    def _targets(self, recipients: list[str], group_id: str | None) -> list[str]:
        """Resolve recipients + group into JIDs, enforcing the allowlist.

        Args:
            recipients: E.164 numbers (allowlist-enforced).
            group_id: Optional WhatsApp group id (always allowed).

        Returns:
            Ordered list of target JIDs.

        Raises:
            WhatsAppNotAllowedError: If a recipient is not allowlisted.
        """
        targets: list[str] = []
        for number in self._normalize_recipients(recipients):
            if not self._is_allowed(number):
                raise WhatsAppNotAllowedError(
                    f"WhatsApp recipient {number} is not in WHATSAPP_ALLOWLIST"
                )
            targets.append(self._jid_for(number))
        if group_id:
            targets.append(self._jid_for_group(group_id))
        return targets

    async def _pace(self, jid: str) -> None:
        """Delay before a send applying cool-down and human typing delay.

        Args:
            jid: Target JID the upcoming message is addressed to.
        """
        now = time.monotonic()
        wait = 0.0
        last = self._last_sent.get(jid)
        cool = self._cooldown_seconds
        if last is not None and cool > 0 and now - last < cool:
            wait = max(wait, cool - (now - last))
        wait = max(wait, self._delay_seconds())
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_sent[jid] = time.monotonic()

    async def _request(self, path: str, payload: dict[str, Any], timeout: float = 60.0) -> None:
        """POST a JSON payload to the Baileys gateway.

        Args:
            path: API path (e.g. /message, /simulate).
            payload: JSON body.
            timeout: HTTP client timeout in seconds.

        Raises:
            WhatsAppConfigError: If the gateway is not configured.
            WhatsAppDeliveryError: If the gateway rejects the request.
        """
        if not self._configured:
            raise WhatsAppConfigError(
                "WhatsApp is not configured; set WHATSAPP_API_TOKEN (and WHATSAPP_API_URL)"
                " in your .env"
            )
        url = self._cfg.whatsapp_api_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self._cfg.whatsapp_api_token}"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
        if not response.is_success:
            raise WhatsAppDeliveryError(
                f"Baileys gateway error for {path}: {response.status_code} {response.text[:200]}"
            )

    @trace_function(component="api.services.whatsapp")
    async def send_text(
        self,
        message: str,
        recipients: list[str],
        group_id: str | None = None,
    ) -> dict[str, str]:
        """Send the digest text to each target, chunked and paced.

        Args:
            message: Raw digest markdown.
            recipients: E.164 numbers.
            group_id: Optional WhatsApp group id.

        Returns:
            Dict with status and recipient count.

        Raises:
            ValueError: If the message is empty or no targets resolve.
            WhatsAppNotAllowedError: If a recipient is not allowlisted.
            WhatsAppDeliveryError: On gateway failure.
        """
        from src.workers.activities import split_message_chunks

        chunks = split_message_chunks(strip_emotion_tags(message), WHATSAPP_MESSAGE_MAX_CHARS)
        if not chunks:
            raise ValueError("WhatsApp message is empty")
        targets = self._targets(recipients, group_id)
        if not targets:
            raise ValueError("No WhatsApp recipients configured")
        typing = self._typing_indicator
        for jid in targets:
            if typing:
                await self._request("/simulate", {"chatId": jid, "action": "composing"})
            for chunk in chunks:
                await self._pace(jid)
                await self._request("/message", {"chatId": jid, "text": chunk})
            if typing:
                await self._request("/simulate", {"chatId": jid, "action": "paused"})
        logger.info("whatsapp_text_sent", recipients=len(targets))
        return {"status": "ok", "recipients": len(targets)}

    @trace_function(component="api.services.whatsapp")
    async def send_audio(
        self,
        audio_path: str,
        recipients: list[str],
        group_id: str | None = None,
        caption: str | None = None,
    ) -> dict[str, str]:
        """Send the compressed M4A audio (base64) to each target.

        Args:
            audio_path: Path to the audio file on disk.
            recipients: E.164 numbers.
            group_id: Optional WhatsApp group id.
            caption: Optional caption text (strips emotion tags).

        Returns:
            Dict with status and recipient count.

        Raises:
            FileNotFoundError: If the audio file is missing.
            ValueError: If the file exceeds the base64 size gate or no targets.
            WhatsAppNotAllowedError: If a recipient is not allowlisted.
            WhatsAppDeliveryError: On gateway failure.
        """
        audio_file = Path(audio_path)
        if not audio_file.is_file():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        raw = audio_file.read_bytes()
        if len(raw) > WHATSAPP_AUDIO_MAX_BYTES:
            raise ValueError(f"Audio file too large for WhatsApp delivery: {len(raw)} bytes")
        targets = self._targets(recipients, group_id)
        if not targets:
            raise ValueError("No WhatsApp recipients configured")
        b64 = base64.b64encode(raw).decode("ascii")
        typing = self._typing_indicator
        clean_caption = strip_emotion_tags(caption) if caption else None
        for jid in targets:
            if typing:
                await self._request("/simulate", {"chatId": jid, "action": "composing"})
            await self._pace(jid)
            media: dict[str, Any] = {
                "type": "audio",
                "filename": audio_file.name,
                "mimetype": "audio/mp4",
                "data": b64,
                "ptt": False,
            }
            if clean_caption:
                media["caption"] = clean_caption
            await self._request("/message", {"chatId": jid, "media": media}, timeout=120.0)
            if typing:
                await self._request("/simulate", {"chatId": jid, "action": "paused"})
        logger.info("whatsapp_audio_sent", recipients=len(targets))
        return {"status": "ok", "recipients": len(targets)}
