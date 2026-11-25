from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class AlertType(str, Enum):
    HISTORICAL_LOW = "HISTORICAL_LOW"
    STRONG_DEAL = "STRONG_DEAL"
    PRICE_ERROR = "PRICE_ERROR"


class PaymentMethodType(str, Enum):
    ALL_PAYMENT_METHODS = "ALL_PAYMENT_METHODS"
    STORE_CARD = "STORE_CARD"


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """Single scraped price observation for a product."""

    current_price: int | None
    in_stock: bool
    normal_price: int | None = None
    parse_ok: bool = True
    payment_method: PaymentMethodType = PaymentMethodType.ALL_PAYMENT_METHODS
    payment_method_label: str = "Todo medio de pago"


@dataclass(frozen=True, slots=True)
class AlertHistoryEntry:
    """Previous alert record used for cooldown evaluation."""

    alert_type: str
    price_triggered: int
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class AnomalyDecision:
    """Output of the anomaly detector for a single price observation."""

    alert_type: AlertType | None
    reason: str
    current_price: int | None
    reference_price: int | None = None
    median_30d: Decimal | None = None
    avg_30d: Decimal | None = None
    min_30d: int | None = None
    history_samples: int = 0
    previous_price: int | None = None
    normal_price: int | None = None
    drop_vs_median_pct: Decimal | None = None
    drop_vs_avg_pct: Decimal | None = None
    drop_vs_previous_pct: Decimal | None = None
    drop_vs_normal_pct: Decimal | None = None
    effective_discount_pct: Decimal | None = None
    is_cold_start: bool = False
    suppressed_by_cooldown: bool = False
    suppressed_alert_type: AlertType | None = None

    @property
    def should_alert(self) -> bool:
        return self.alert_type is not None
