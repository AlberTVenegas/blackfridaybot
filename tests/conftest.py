from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from analyzer.detector import PriceAnomalyDetector
from config.settings import Settings


@pytest.fixture
def base_now() -> datetime:
    return datetime(2026, 11, 27, 15, 0, 0, tzinfo=UTC)


@pytest.fixture
def detector() -> PriceAnomalyDetector:
    return PriceAnomalyDetector(
        extreme_discount_threshold=Decimal(70),
        historical_low_min_drop=Decimal(10),
        cooldown_hours=6,
        cold_start_absurd_price_clp=1000,
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://postgres:test@localhost:5432/test",
        telegram_bot_token="123456:TEST_TOKEN",
        telegram_chat_id="999999",
        twilio_enabled=True,
        twilio_account_sid="AC00000000000000000000000000000000",
        twilio_auth_token="test_auth_token",
        twilio_from_number="+15550001111",
        twilio_to_number="+56912345678",
        twilio_global_cooldown_seconds=180,
        request_min_delay_sec=0.0,
        request_max_delay_sec=0.0,
        include_store_card_prices=True,
    )
