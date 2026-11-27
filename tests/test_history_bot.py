from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from analyzer.models import AlertType, PaymentMethodType
from notifier.base import AlertPayload
from notifier.telegram import TelegramNotifier
from notifier.telegram_bot import _collapse_price_timeline, format_price_history_message


def test_collapse_price_timeline_keeps_changes_and_endpoints() -> None:
    t1 = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    t3 = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    t4 = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    history = [
        {"price": 100_000, "scraped_at": t1, "payment_method": "ALL_PAYMENT_METHODS"},
        {"price": 100_000, "scraped_at": t2, "payment_method": "ALL_PAYMENT_METHODS"},
        {"price": 200_000, "scraped_at": t3, "payment_method": "ALL_PAYMENT_METHODS"},
        {"price": 200_000, "scraped_at": t4, "payment_method": "ALL_PAYMENT_METHODS"},
    ]
    collapsed = _collapse_price_timeline(history)
    # Newest ($100k @ t1), transition ($200k @ t3), oldest ($200k @ t4)
    assert len(collapsed) == 3
    assert [c["price"] for c in collapsed] == [100_000, 200_000, 200_000]


def test_format_price_history_message_includes_stats_and_timeline() -> None:
    data = {
        "id": 42,
        "sku": "SKU-42",
        "name": "Galaxy S25 Ultra 256GB",
        "retailer_name": "Paris",
        "url": "https://www.paris.cl/s25.html",
        "history": [
            {
                "price": 500_000,
                "scraped_at": datetime(2026, 11, 27, 3, 15, tzinfo=UTC),
                "payment_method": "STORE_CARD",
            },
            {
                "price": 1_000_000,
                "scraped_at": datetime(2026, 11, 26, 15, 0, tzinfo=UTC),
                "payment_method": "ALL_PAYMENT_METHODS",
            },
        ],
    }
    msg = format_price_history_message(data)
    assert "HISTORIAL DE PRECIOS" in msg
    assert "Galaxy S25 Ultra" in msg
    assert "500\\.000" in msg
    assert "750\\.000" in msg
    assert "1\\.000\\.000" in msg
    assert "27/11 00:15" in msg  # UTC-3 Chile time


@pytest.mark.asyncio
@respx.mock
async def test_telegram_alert_includes_inline_history_button() -> None:
    route = respx.post("https://api.telegram.org/botTEST/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        tg = TelegramNotifier(bot_token="TEST", chat_id="-100123", client=client)
        payload = AlertPayload(
            retailer_name="Falabella",
            product_name="Notebook Test",
            sku="NB-1",
            url="https://www.falabella.com/p/1",
            alert_type=AlertType.STRONG_DEAL,
            current_price=300_000,
            reference_price=900_000,
            discount_pct=Decimal("66.7"),
            payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
            product_id=777,
        )
        ok = await tg.send(payload)
        assert ok is True
        sent_json = route.calls[0].request.content.decode()
        assert "hist:777" in sent_json
        assert "Ver historial de precios" in sent_json
