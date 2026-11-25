from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from analyzer.models import AlertType
from notifier.base import AlertPayload, DispatchResult
from notifier.telegram import TelegramNotifier
from notifier.twilio_voice import TwilioVoiceNotifier

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """Routes anomaly alerts across Telegram and Twilio Voice channels.

    - HISTORICAL_LOW -> Telegram only.
    - PRICE_ERROR    -> Twilio Voice (with global rate limit) + Telegram in parallel.
                        Telegram is ALWAYS attempted even if Twilio is rate-limited or fails.
    """

    def __init__(
        self,
        *,
        telegram: TelegramNotifier,
        twilio: TwilioVoiceNotifier | None = None,
    ) -> None:
        self.telegram = telegram
        self.twilio = twilio

    async def dispatch(
        self,
        payload: AlertPayload,
        *,
        now: datetime | None = None,
    ) -> DispatchResult:
        result = DispatchResult()

        if payload.alert_type != AlertType.PRICE_ERROR:
            try:
                result.telegram_sent = await self.telegram.send(payload)
            except Exception as exc:
                logger.exception("Telegram delivery failed for %s", payload.sku)
                result.errors.append(f"telegram: {exc}")
            return result

        # Level 2: PRICE_ERROR -> Parallel Twilio Voice + Priority Telegram
        async def _run_telegram() -> bool:
            return await self.telegram.send(payload)

        async def _run_twilio() -> tuple[bool, bool]:
            if self.twilio is None:
                return False, False
            return await self.twilio.send_with_status(payload, now=now)

        tg_outcome, tw_outcome = await asyncio.gather(
            _run_telegram(),
            _run_twilio(),
            return_exceptions=True,
        )

        if isinstance(tg_outcome, Exception):
            logger.error(
                "Telegram priority delivery failed for %s: %s",
                payload.sku,
                tg_outcome,
                exc_info=tg_outcome,
            )
            result.errors.append(f"telegram: {tg_outcome}")
        else:
            result.telegram_sent = tg_outcome

        if isinstance(tw_outcome, Exception):
            logger.error(
                "Twilio voice call failed for %s: %s",
                payload.sku,
                tw_outcome,
                exc_info=tw_outcome,
            )
            result.errors.append(f"twilio: {tw_outcome}")
        else:
            called, rate_limited = tw_outcome
            result.twilio_called = called
            result.twilio_rate_limited = rate_limited

        return result
