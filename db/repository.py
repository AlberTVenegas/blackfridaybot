from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AlertSent, PriceHistory, Product, Retailer


@dataclass(frozen=True, slots=True)
class ProductPriceContext:
    """Historical price snapshot retrieved before inserting a new observation."""

    history_prices: list[int]
    previous_price: int | None


@dataclass(frozen=True, slots=True)
class RecentAlertRecord:
    """Lightweight representation of a previously sent alert for cooldown checks."""

    alert_type: str
    price_triggered: int
    sent_at: datetime


class PriceRepository:
    """Async repository encapsulating all PostgreSQL queries for price monitoring."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_retailer(self, name: str, base_url: str) -> Retailer:
        stmt = (
            pg_insert(Retailer)
            .values(name=name, base_url=base_url)
            .on_conflict_do_update(
                index_elements=[Retailer.name],
                set_={"base_url": base_url},
            )
            .returning(Retailer)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def upsert_product(
        self,
        *,
        retailer_id: int,
        sku: str,
        name: str,
        url: str,
        category: str | None = None,
        is_active: bool = True,
    ) -> Product:
        stmt = (
            pg_insert(Product)
            .values(
                retailer_id=retailer_id,
                sku=sku,
                name=name,
                category=category,
                url=url,
                is_active=is_active,
            )
            .on_conflict_do_update(
                constraint="uq_products_retailer_sku",
                set_={
                    "name": name,
                    "category": category,
                    "url": url,
                    "is_active": is_active,
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(Product)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_price_context(
        self,
        product_id: int,
        *,
        window_days: int = 30,
        now: datetime | None = None,
    ) -> ProductPriceContext:
        """Fetch valid positive prices in the last `window_days` and the latest price."""
        reference_time = now or datetime.now(UTC)
        cutoff = reference_time - timedelta(days=window_days)

        # Prices within window (ordered newest first)
        stmt = (
            select(PriceHistory.price)
            .where(
                PriceHistory.product_id == product_id,
                PriceHistory.scraped_at >= cutoff,
                PriceHistory.price > 0,
            )
            .order_by(desc(PriceHistory.scraped_at))
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        history_prices = list(rows)

        if history_prices:
            previous_price: int | None = history_prices[0]
        else:
            # Fallback: check if there's an older valid observation outside 30d window
            last_stmt = (
                select(PriceHistory.price)
                .where(
                    PriceHistory.product_id == product_id,
                    PriceHistory.price > 0,
                )
                .order_by(desc(PriceHistory.scraped_at))
                .limit(1)
            )
            previous_price = (await self._session.execute(last_stmt)).scalar_one_or_none()

        return ProductPriceContext(
            history_prices=history_prices,
            previous_price=previous_price,
        )

    async def record_price(
        self,
        *,
        product_id: int,
        price: int,
        previous_price: int | None,
        in_stock: bool,
        normal_price: int | None = None,
        payment_method: str = "ALL_PAYMENT_METHODS",
        payment_method_label: str | None = None,
        scraped_at: datetime | None = None,
    ) -> PriceHistory:
        entry = PriceHistory(
            product_id=product_id,
            price=price,
            previous_price=previous_price,
            normal_price=normal_price,
            payment_method=payment_method,
            payment_method_label=payment_method_label,
            in_stock=in_stock,
            scraped_at=scraped_at or datetime.now(UTC),
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def get_recent_alerts(
        self,
        product_id: int,
        *,
        cooldown_hours: int = 6,
        now: datetime | None = None,
    ) -> list[RecentAlertRecord]:
        if cooldown_hours <= 0:
            return []
        reference_time = now or datetime.now(UTC)
        cutoff = reference_time - timedelta(hours=cooldown_hours)
        stmt = (
            select(
                AlertSent.alert_type,
                AlertSent.price_triggered,
                AlertSent.sent_at,
            )
            .where(
                AlertSent.product_id == product_id,
                AlertSent.sent_at >= cutoff,
            )
            .order_by(desc(AlertSent.sent_at))
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            RecentAlertRecord(
                alert_type=row.alert_type,
                price_triggered=row.price_triggered,
                sent_at=row.sent_at,
            )
            for row in rows
        ]

    async def record_alert(
        self,
        *,
        product_id: int,
        alert_type: str,
        price_triggered: int,
        twilio_called: bool = False,
        sent_at: datetime | None = None,
    ) -> AlertSent:
        alert = AlertSent(
            product_id=product_id,
            alert_type=alert_type,
            price_triggered=price_triggered,
            twilio_called=twilio_called,
            sent_at=sent_at or datetime.now(UTC),
        )
        self._session.add(alert)
        await self._session.flush()
        return alert

    async def get_last_twilio_call_at(self) -> datetime | None:
        """Return timestamp of the most recent Twilio voice call across all products."""
        stmt = (
            select(AlertSent.sent_at)
            .where(AlertSent.twilio_called.is_(True))
            .order_by(desc(AlertSent.sent_at))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
