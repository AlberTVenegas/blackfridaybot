from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from analyzer.models import AlertType, PaymentMethodType
from notifier.base import AlertPayload
from notifier.callmebot_voice import CALLMEBOT_API_URL, CallMeBotVoiceNotifier


def _sample_payload() -> AlertPayload:
    return AlertPayload(
        retailer_name="Falabella",
        product_name="Notebook Gamer RTX 4060 16GB RAM",
        sku="SKU-999",
        url="https://www.falabella.com/falabella-cl/product/999",
        category="Notebooks",
        alert_type=AlertType.PRICE_ERROR,
        current_price=9990,
        reference_price=999990,
        reference_label="Mediana 30 días",
        discount_pct=Decimal("99.00"),
        payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
        payment_method_label="Todo medio de pago",
        all_payment_price_clp=9990,
        is_cold_start=False,
        reason="median_30d_extreme_drop",
    )


def test_callmebot_spoken_text_under_256_chars() -> None:
    notifier = CallMeBotVoiceNotifier(telegram_user="@Albert_del_rey")
    text = notifier.build_spoken_text(_sample_payload())
    assert len(text) <= 256
    assert "Falabella" in text
    assert "$9.990" in text


@pytest.mark.asyncio
@respx.mock
async def test_callmebot_success_and_global_cooldown() -> None:
    route = respx.get(CALLMEBOT_API_URL).mock(
        return_value=httpx.Response(200, text="Autorization OK. Call Placed.")
    )
    async with httpx.AsyncClient() as client:
        notifier = CallMeBotVoiceNotifier(
            telegram_user="@Albert_del_rey",
            global_cooldown_seconds=180,
            client=client,
        )
        t0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
        called1, limited1 = await notifier.send_with_status(_sample_payload(), now=t0)
        assert called1 is True
        assert limited1 is False
        assert route.call_count == 1

        # Second call within cooldown is suppressed
        called2, limited2 = await notifier.send_with_status(
            _sample_payload(), now=t0 + timedelta(seconds=60)
        )
        assert called2 is False
        assert limited2 is True
        assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_callmebot_detects_flood_response() -> None:
    respx.get(CALLMEBOT_API_URL).mock(
        return_value=httpx.Response(
            200, text="Error: FLOOD - Too many users making calls at this time. This call was skipped."
        )
    )
    async with httpx.AsyncClient() as client:
        notifier = CallMeBotVoiceNotifier(telegram_user="@Albert_del_rey", client=client)
        called, limited = await notifier.send_with_status(_sample_payload())
        assert called is False
        assert limited is True
