from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from analyzer.models import AlertType, PaymentMethodType


def format_clp(amount: int) -> str:
    """Format an integer amount in Chilean Pesos, e.g. 1299990 -> '$1.299.990'."""
    return f"${amount:,}".replace(",", ".")


@dataclass(frozen=True, slots=True)
class AlertPayload:
    """Structured notification payload passed to alerting channels."""

    retailer_name: str
    product_name: str
    sku: str
    url: str
    alert_type: AlertType
    current_price: int
    reference_price: int | None = None
    reference_label: str = "Mediana 30 días"
    discount_pct: Decimal | None = None
    category: str | None = None
    payment_method: PaymentMethodType = PaymentMethodType.ALL_PAYMENT_METHODS
    payment_method_label: str = "Todo medio de pago"
    all_payment_price_clp: int | None = None
    is_cold_start: bool = False
    reason: str = ""


@dataclass(slots=True)
class DispatchResult:
    """Summary of notification delivery across channels."""

    telegram_sent: bool = False
    twilio_called: bool = False
    twilio_rate_limited: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def any_sent(self) -> bool:
        return self.telegram_sent or self.twilio_called


class BaseNotifier(ABC):
    """Abstract base class for alert notification channels."""

    @abstractmethod
    async def send(self, payload: AlertPayload) -> bool:
        """Send an alert notification. Returns True if delivered."""
