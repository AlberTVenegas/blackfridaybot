from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape as xml_escape

import httpx

from analyzer.models import AlertType
from notifier.base import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)


class TwilioVoiceNotifier(BaseNotifier):
    """Async Twilio Voice API notifier with global call rate-limiting.

    Uses inline TwiML via Twilio's REST API so no public webhook server is required,
    and guards concurrent bursts with an asyncio.Lock + global cooldown window.
    """

    API_BASE = "https://api.twilio.com/2010-04-01"

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        voice_name: str = "Polly.Mia",
        language: str = "es-MX",
        global_cooldown_seconds: int = 180,
        enabled: bool = True,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._account_sid = account_sid.strip()
        self._auth_token = auth_token.strip()
        self._from_number = from_number.strip()
        self._to_number = to_number.strip()
        self._voice_name = voice_name
        self._language = language
        self._global_cooldown_seconds = max(0, global_cooldown_seconds)
        self._enabled = enabled
        self._timeout = timeout
        self._external_client = client

        self._lock = asyncio.Lock()
        self._last_call_at: datetime | None = None

    @property
    def is_configured(self) -> bool:
        return bool(
            self._enabled
            and self._account_sid
            and self._auth_token
            and self._from_number
            and self._to_number
        )

    def seed_last_call_at(self, timestamp: datetime | None) -> None:
        """Hydrate last call timestamp (e.g. from Supabase on startup)."""
        if timestamp is None:
            return
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if self._last_call_at is None or timestamp > self._last_call_at:
            self._last_call_at = timestamp

    def build_twiml(self, payload: AlertPayload) -> str:
        """Build XML-safe Spanish TwiML voice message."""
        store = xml_escape(payload.retailer_name)
        if payload.discount_pct is not None:
            pct_int = int(payload.discount_pct)
            spoken = (
                f"Alerta de descuento extremo en {store}. "
                f"Descuento del {pct_int} por ciento. "
                "Revisa Telegram ahora."
            )
        else:
            spoken = (
                f"Alerta de error de precio extremo en {store}. "
                f"Precio detectado de {payload.current_price} pesos. "
                "Revisa Telegram ahora."
            )
        voice_attr = xml_escape(self._voice_name)
        lang_attr = xml_escape(self._language)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Say voice="{voice_attr}" language="{lang_attr}">'
            f"{spoken}"
            "</Say></Response>"
        )

    async def send_with_status(
        self,
        payload: AlertPayload,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, bool]:
        """Attempt a voice call respecting the global rate limit.

        Returns:
            (called, rate_limited)
        """
        if payload.alert_type != AlertType.PRICE_ERROR:
            return False, False

        if not self.is_configured:
            logger.warning("TwilioVoiceNotifier is not configured/enabled; skipping call.")
            return False, False

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        # Atomic check-and-reserve inside lock to prevent concurrent call storms
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
                logger.info(
                    "Twilio global rate limit active (%ss remaining). "
                    "Skipping call for %s (%s); Telegram alert still delivered.",
                    remaining,
                    payload.retailer_name,
                    payload.sku,
                )
                return False, True

            previous_last_call = self._last_call_at
            self._last_call_at = current_time

        endpoint = f"{self.API_BASE}/Accounts/{self._account_sid}/Calls.json"
        form_data = {
            "To": self._to_number,
            "From": self._from_number,
            "Twiml": self.build_twiml(payload),
        }

        try:
            if self._external_client is not None:
                resp = await self._external_client.post(
                    endpoint,
                    data=form_data,
                    auth=(self._account_sid, self._auth_token),
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        endpoint,
                        data=form_data,
                        auth=(self._account_sid, self._auth_token),
                    )
            resp.raise_for_status()
            logger.info(
                "Twilio voice call initiated to %s for %s (%s)",
                self._to_number,
                payload.retailer_name,
                payload.sku,
            )
            return True, False
        except Exception:
            # Roll back reservation on API failure so the next critical alert can retry
            async with self._lock:
                if self._last_call_at == current_time:
                    self._last_call_at = previous_last_call
            raise

    async def send(self, payload: AlertPayload) -> bool:
        called, _ = await self.send_with_status(payload)
        return called
