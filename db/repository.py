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

    async def upsert_products_batch(
        self,
        *,
        retailer_id: int,
        items: list[dict[str, object]],
    ) -> dict[str, int]:
        """Bulk upsert products for a retailer and return {sku: product_id}."""
        if not items:
            return {}
        # Deduplicate by sku within batch (Postgres ON CONFLICT rejects duplicate keys in same stmt)
        deduped: dict[str, dict[str, object]] = {}
        now = datetime.now(UTC)
        for item in items:
            sku = str(item["sku"])
            deduped[sku] = {
                "retailer_id": retailer_id,
                "sku": sku,
                "name": item["name"],
                "category": item.get("category"),
                "url": item["url"],
                "is_active": True,
                "updated_at": now,
            }
        values = list(deduped.values())
        stmt = pg_insert(Product).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_products_retailer_sku",
            set_={
                "name": stmt.excluded.name,
                "category": stmt.excluded.category,
                "url": stmt.excluded.url,
                "is_active": stmt.excluded.is_active,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(Product.sku, Product.id)
        rows = (await self._session.execute(stmt)).all()
        return {row.sku: row.id for row in rows}

    async def get_price_stats_batch(
        self,
        product_ids: list[int],
        *,
        window_days: int = 30,
        now: datetime | None = None,
    ) -> dict[int, tuple[object, object, int | None, int, int | None, datetime | None]]:
        """Compute 30d median, avg, min, sample count, latest price, and last timestamp in ONE SQL query.

        Returns:
            {product_id: (median_30d, avg_30d, min_30d, samples, previous_price, last_scraped_at)}
        """
        if not product_ids:
            return {}
        from decimal import Decimal

        from sqlalchemy import text

        reference_time = now or datetime.now(UTC)
        cutoff = reference_time - timedelta(days=window_days)

        sql = text(
            """
            SELECT
                product_id,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_30d,
                AVG(price) AS avg_30d,
                MIN(price) AS min_30d,
                COUNT(*) AS samples,
                (ARRAY_AGG(price ORDER BY scraped_at DESC))[1] AS previous_price,
                MAX(scraped_at) AS last_scraped_at
            FROM price_history
            WHERE product_id = ANY(:product_ids)
              AND scraped_at >= :cutoff
              AND price > 0
            GROUP BY product_id
            """
        )
        rows = (
            await self._session.execute(
                sql,
                {"product_ids": product_ids, "cutoff": cutoff},
            )
        ).all()
        stats_map: dict[int, tuple[object, object, int | None, int, int | None, datetime | None]] = {}
        for row in rows:
            median_val = Decimal(str(row.median_30d)) if row.median_30d is not None else None
            avg_val = Decimal(str(row.avg_30d)) if row.avg_30d is not None else None
            stats_map[int(row.product_id)] = (
                median_val,
                avg_val,
                int(row.min_30d) if row.min_30d is not None else None,
                int(row.samples or 0),
                int(row.previous_price) if row.previous_price is not None else None,
                row.last_scraped_at,
            )
        return stats_map

    async def get_recent_alerts_batch(
        self,
        product_ids: list[int],
        *,
        cooldown_hours: int = 6,
        now: datetime | None = None,
    ) -> dict[int, list[RecentAlertRecord]]:
        """Fetch recent alerts for a batch of product IDs in ONE SQL query."""
        if not product_ids or cooldown_hours <= 0:
            return {}
        reference_time = now or datetime.now(UTC)
        cutoff = reference_time - timedelta(hours=cooldown_hours)
        stmt = (
            select(
                AlertSent.product_id,
                AlertSent.alert_type,
                AlertSent.price_triggered,
                AlertSent.sent_at,
            )
            .where(
                AlertSent.product_id.in_(product_ids),
                AlertSent.sent_at >= cutoff,
            )
            .order_by(desc(AlertSent.sent_at))
        )
        rows = (await self._session.execute(stmt)).all()
        grouped: dict[int, list[RecentAlertRecord]] = {}
        for row in rows:
            grouped.setdefault(int(row.product_id), []).append(
                RecentAlertRecord(
                    alert_type=row.alert_type,
                    price_triggered=row.price_triggered,
                    sent_at=row.sent_at,
                )
            )
        return grouped

    async def record_prices_batch(
        self,
        entries: list[dict[str, object]],
    ) -> int:
        """Bulk insert price observations in ONE SQL statement."""
        if not entries:
            return 0
        await self._session.execute(pg_insert(PriceHistory).values(entries))
        return len(entries)

    async def prune_old_history(
        self,
        *,
        retention_days: int = 7,
        now: datetime | None = None,
    ) -> int:
        """Delete price_history rows older than `retention_days` to keep DB lean."""
        from sqlalchemy import delete

        reference_time = now or datetime.now(UTC)
        cutoff = reference_time - timedelta(days=max(1, retention_days))
        stmt = delete(PriceHistory).where(PriceHistory.scraped_at < cutoff)
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)
