from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from analyzer.models import AlertType, PaymentMethodType
from notifier.base import AlertPayload
from notifier.dispatcher import AlertDispatcher
from notifier.telegram import TelegramNotifier
from notifier.twilio_voice import TwilioVoiceNotifier


def _make_payload(
    *,
    alert_type: AlertType = AlertType.PRICE_ERROR,
    sku: str = "SKU-1",
    retailer: str = "Falabella",
    current_price: int = 100,
    reference_price: int | None = 100_000,
    discount_pct: Decimal | None = Decimal("99.90"),
    payment_method: PaymentMethodType = PaymentMethodType.ALL_PAYMENT_METHODS,
    payment_label: str = "Todo medio de pago",
    all_payment_price: int | None = None,
    is_cold_start: bool = False,
) -> AlertPayload:
    return AlertPayload(
        retailer_name=retailer,
        product_name="Notebook Pro 16 (M3) - 512GB!",
        sku=sku,
        url="https://www.falabella.com/falabella-cl/product/12345/notebook",
        category="Notebooks",
        alert_type=alert_type,
        current_price=current_price,
        reference_price=reference_price,
        discount_pct=discount_pct,
        payment_method=payment_method,
        payment_method_label=payment_label,
        all_payment_price_clp=all_payment_price,
        is_cold_start=is_cold_start,
    )


def test_telegram_markdown_formatting_includes_payment_method_and_cold_start() -> None:
    tg = TelegramNotifier(bot_token="tok", chat_id="123")
    payload = _make_payload(
        payment_method=PaymentMethodType.STORE_CARD,
        payment_label="Exclusivo Tarjeta CMR",
        all_payment_price=49_990,
        is_cold_start=True,
    )
    msg = tg.format_message(payload)

    assert "ERROR DE PRECIO" in msg
    assert "Falabella" in msg
    assert "Exclusivo Tarjeta CMR" in msg
    assert "49\\.990 CLP" in msg
    assert "arranque en frío" in msg
    assert "https://www.falabella.com/falabella-cl/product/12345/notebook" in msg


def test_twilio_twiml_is_valid_spanish_xml() -> None:
    twilio = TwilioVoiceNotifier(
        account_sid="AC123",
        auth_token="secret",
        from_number="+15550001111",
        to_number="+56912345678",
    )
    payload = _make_payload(retailer="Tienda A & B", discount_pct=Decimal("99.90"))
    twiml = twilio.build_twiml(payload)

    root = ET.fromstring(twiml)
    assert root.tag == "Response"
    say = root.find("Say")
    assert say is not None
    assert say.attrib["language"] == "es-MX"
    assert "Tienda A & B" in (say.text or "")
    assert "99 por ciento" in (say.text or "")
    assert "Revisa Telegram ahora" in (say.text or "")


@pytest.mark.asyncio
async def test_dispatcher_historical_low_calls_telegram_only() -> None:
    with respx.mock(assert_all_called=False) as mock:
        tg_route = mock.post("https://api.telegram.org/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        tw_route = mock.post("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json").mock(
            return_value=httpx.Response(201, json={"sid": "CA123"})
        )

        async with httpx.AsyncClient() as client:
            dispatcher = AlertDispatcher(
                telegram=TelegramNotifier(bot_token="tok", chat_id="123", client=client),
                twilio=TwilioVoiceNotifier(
                    account_sid="AC123",
                    auth_token="secret",
                    from_number="+15550001111",
                    to_number="+56912345678",
                    client=client,
                ),
            )
            result = await dispatcher.dispatch(
                _make_payload(alert_type=AlertType.HISTORICAL_LOW, current_price=75_000, discount_pct=Decimal("25.00"))
            )

        assert result.telegram_sent is True
        assert result.twilio_called is False
        assert tg_route.call_count == 1
        assert tw_route.call_count == 0


@pytest.mark.asyncio
async def test_twilio_global_rate_limit_blocks_storm_while_all_telegram_alerts_send(
    base_now: datetime,
) -> None:
    """5 concurrent PRICE_ERROR alerts -> 1 Twilio call max, 5 Telegram messages delivered."""
    with respx.mock() as mock:
        tg_route = mock.post("https://api.telegram.org/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        tw_route = mock.post("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json").mock(
            return_value=httpx.Response(201, json={"sid": "CA123"})
        )

        async with httpx.AsyncClient() as client:
            twilio = TwilioVoiceNotifier(
                account_sid="AC123",
                auth_token="secret",
                from_number="+15550001111",
                to_number="+56912345678",
                global_cooldown_seconds=180,
                client=client,
            )
            dispatcher = AlertDispatcher(
                telegram=TelegramNotifier(bot_token="tok", chat_id="123", client=client),
                twilio=twilio,
            )

            payloads = [_make_payload(sku=f"SKU-{i}") for i in range(5)]
            results = await asyncio.gather(
                *(dispatcher.dispatch(p, now=base_now) for p in payloads)
            )

            assert tg_route.call_count == 5
            assert tw_route.call_count == 1
            assert all(r.telegram_sent for r in results)
            assert sum(1 for r in results if r.twilio_called) == 1
            assert sum(1 for r in results if r.twilio_rate_limited) == 4

            # After 181 seconds (> 3 min global cooldown), a new Twilio call IS allowed
            later_result = await dispatcher.dispatch(
                _make_payload(sku="SKU-LATER"),
                now=base_now + timedelta(seconds=181),
            )
            assert later_result.telegram_sent is True
            assert later_result.twilio_called is True
            assert later_result.twilio_rate_limited is False
            assert tw_route.call_count == 2
            assert tg_route.call_count == 6


@pytest.mark.asyncio
async def test_twilio_failure_rolls_back_rate_limit_and_keeps_telegram_working(
    base_now: datetime,
) -> None:
    with respx.mock() as mock:
        mock.post("https://api.telegram.org/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        tw_route = mock.post("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json").mock(
            side_effect=[
                httpx.Response(500, json={"message": "Twilio error"}),
                httpx.Response(201, json={"sid": "CA999"}),
            ]
        )

        async with httpx.AsyncClient() as client:
            dispatcher = AlertDispatcher(
                telegram=TelegramNotifier(bot_token="tok", chat_id="123", client=client),
                twilio=TwilioVoiceNotifier(
                    account_sid="AC123",
                    auth_token="secret",
                    from_number="+15550001111",
                    to_number="+56912345678",
                    global_cooldown_seconds=180,
                    client=client,
                ),
            )

            first = await dispatcher.dispatch(_make_payload(sku="SKU-FAIL"), now=base_now)
            assert first.telegram_sent is True
            assert first.twilio_called is False
            assert len(first.errors) == 1

            # Next alert 5 seconds later should still be allowed to call Twilio since first failed
            second = await dispatcher.dispatch(
                _make_payload(sku="SKU-RETRY"), now=base_now + timedelta(seconds=5)
            )
            assert second.telegram_sent is True
            assert second.twilio_called is True
            assert tw_route.call_count == 2
