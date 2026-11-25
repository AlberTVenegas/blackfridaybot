from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from notifier.base import AlertPayload, BaseNotifier, format_clp

logger = logging.getLogger(__name__)

CALLMEBOT_API_URL = "https://api.callmebot.com/start.php"
MAX_TTS_LENGTH = 256


class CallMeBotVoiceNotifier(BaseNotifier):
    """Free Telegram voice-call notifier via CallMeBot API (HTTP GET).

    Mirrors TwilioVoiceNotifier's interface (`seed_last_call_at`, `send_with_status`,
    `send`) and enforces an `asyncio.Lock` + global cooldown window (>= 65s) so
    massive price-error bursts never trigger CallMeBot's flood protection.
    """

    def __init__(
        self,
        *,
        telegram_user: str,
        lang: str = "es-US-Standard-A",
        repeat: int = 2,
        global_cooldown_seconds: int = 180,
        enabled: bool = True,
        timeout: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user = telegram_user.strip()
        self._lang = lang.strip() or "es-US-Standard-A"
        self._repeat = max(1, min(5, repeat))
        self._global_cooldown_seconds = max(65, global_cooldown_seconds)
        self._enabled = enabled
        self._timeout = timeout
        self._client = client
        self._lock = asyncio.Lock()
        self._last_call_at: datetime | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._enabled and self._user)

    def seed_last_call_at(self, timestamp: datetime | None) -> None:
        if timestamp is None:
            return
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if self._last_call_at is None or timestamp > self._last_call_at:
            self._last_call_at = timestamp

    def build_spoken_text(self, payload: AlertPayload) -> str:
        """Build a concise Spanish TTS alert (max 256 chars required by CallMeBot)."""
        price_spoken = format_clp(payload.current_price)
        short_name = payload.product_name[:70].strip()
        discount_part = (
            f" con {payload.discount_pct:.0f} por ciento de descuento"
            if payload.discount_pct is not None
            else ""
        )
        msg = (
            f"Alerta de error de precio en {payload.retailer_name}. "
            f"{short_name} a {price_spoken} pesos{discount_part}. "
            "Revisa tu Telegram ahora."
        )
        return msg[:MAX_TTS_LENGTH]

    async def send_with_status(
        self,
        payload: AlertPayload,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, bool]:
        """Attempt a CallMeBot Telegram voice call respecting global cooldown.

        Returns:
            (called, rate_limited)
        """
        if not self.is_configured:
            logger.warning("CallMeBotVoiceNotifier is not configured/enabled; skipping call.")
            return False, False

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        previous_last_call: datetime | None = None
        async with self._lock:
            if (
                self._global_cooldown_seconds > 0
                and self._last_call_at is not None
                and (current_time - self._last_call_at)
                < timedelta(seconds=self._global_cooldown_seconds)
            ):
                remaining = self._global_cooldown_seconds - int(
                    (current_time - self._last_call_at).total_seconds()
                )
                logger.warning(
                    "CallMeBot global cooldown active (%ds remaining); skipping call for %s (%s).",
                    max(remaining, 0),
                    payload.retailer_name,
                    payload.sku,
                )
                return False, True

            previous_last_call = self._last_call_at
            self._last_call_at = current_time

        params = {
            "user": self._user,
            "text": self.build_spoken_text(payload),
            "lang": self._lang,
            "rpt": str(self._repeat),
            "cc": "no",
        }

        try:
            if self._client is not None:
                resp = await self._client.get(
                    CALLMEBOT_API_URL,
                    params=params,
                    follow_redirects=True,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(
                        CALLMEBOT_API_URL,
                        params=params,
                        follow_redirects=True,
                    )
            resp.raise_for_status()
            body = resp.text
            lowered = body.lower()
            if "error:" in lowered or "flood" in lowered or "skipped" in lowered:
                logger.warning(
                    "CallMeBot returned an application-level error for %s (%s): %s",
                    payload.retailer_name,
                    payload.sku,
                    body[-300:],
                )
                # Keep _last_call_at reserved on FLOOD/rate-limit to respect CallMeBot's 65s guard
                return False, "flood" in lowered or "65 seconds" in lowered

            logger.info(
                "CallMeBot voice call initiated for %s (%s) to %s",
                payload.retailer_name,
                payload.sku,
                self._user,
            )
            return True, False
        except Exception:
            async with self._lock:
                if self._last_call_at == current_time:
                    self._last_call_at = previous_last_call
            raise

    async def send(self, payload: AlertPayload) -> bool:
        called, _ = await self.send_with_status(payload)
        return called
