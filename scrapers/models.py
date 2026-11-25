from __future__ import annotations

from dataclasses import dataclass

from analyzer.models import PaymentMethodType


@dataclass(frozen=True, slots=True)
class ScrapedProduct:
    """Normalized product payload returned by every retailer scraper."""

    retailer_name: str
    sku: str
    name: str
    url: str
    price_clp: int | None
    in_stock: bool
    category: str | None = None
    normal_price_clp: int | None = None
    all_payment_price_clp: int | None = None
    card_price_clp: int | None = None
    payment_method: PaymentMethodType = PaymentMethodType.ALL_PAYMENT_METHODS
    payment_method_label: str = "Todo medio de pago"
    parse_ok: bool = True
