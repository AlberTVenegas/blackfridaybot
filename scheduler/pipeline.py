from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from analyzer.detector import PriceAnomalyDetector
from analyzer.models import AlertHistoryEntry, PriceObservation
from config.settings import Settings
from db.repository import PriceRepository
from db.session import DatabaseManager
from notifier.base import AlertPayload
from notifier.dispatcher import AlertDispatcher
from scrapers.models import ScrapedProduct
from scrapers.registry import create_scraper

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CategoryTarget:
    name: str
    url: str
    max_pages: int = 2


@dataclass(frozen=True, slots=True)
class WatchlistTarget:
    url: str
    name: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class StoreTargetConfig:
    slug: str
    enabled: bool
    categories: list[CategoryTarget] = field(default_factory=list)
    watchlist: list[WatchlistTarget] = field(default_factory=list)


@dataclass(slots=True)
class CycleStats:
    products_scraped: int = 0
    prices_recorded: int = 0
    alerts_triggered: int = 0
    twilio_calls: int = 0
    errors: int = 0


def load_targets_config(path: Path) -> list[StoreTargetConfig]:
    """Load hybrid categories + watchlist configuration from YAML."""
    if not path.exists():
        logger.warning("Targets file %s not found; returning empty targets list.", path)
        return []

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stores_section = raw.get("stores", {}) or {}
    configs: list[StoreTargetConfig] = []

    for slug, cfg in stores_section.items():
        if not isinstance(cfg, dict):
            continue
        categories = [
            CategoryTarget(
                name=str(c["name"]),
                url=str(c["url"]),
                max_pages=int(c.get("max_pages", 2)),
            )
            for c in (cfg.get("categories") or [])
            if isinstance(c, dict) and c.get("url")
        ]
        watchlist = [
            WatchlistTarget(
                url=str(w["url"]),
                name=w.get("name"),
                category=w.get("category"),
            )
            for w in (cfg.get("watchlist") or [])
            if isinstance(w, dict) and w.get("url")
        ]
        configs.append(
            StoreTargetConfig(
                slug=slug,
                enabled=bool(cfg.get("enabled", False)),
                categories=categories,
                watchlist=watchlist,
            )
        )
    return configs


def _reference_label_for_reason(reason: str) -> str:
    if reason in ("cold_start_normal_price_drop", "cold_start_absurd_price"):
        return "Precio normal tienda"
    if reason == "previous_price_extreme_drop":
        return "Precio anterior"
    return "Mediana 60 días"


class MonitoringPipeline:
    """End-to-end async pipeline: scrape -> persist -> analyze -> notify."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: DatabaseManager,
        detector: PriceAnomalyDetector,
        dispatcher: AlertDispatcher,
    ) -> None:
        self.settings = settings
        self.db = db
        self.detector = detector
        self.dispatcher = dispatcher
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._db_semaphore = asyncio.Semaphore(
            max(4, min(12, settings.db_pool_size + settings.db_max_overflow - 2))
        )

    async def hydrate_global_rate_limits(self) -> None:
        """Seed Twilio's in-memory global cooldown from Supabase on startup."""
        if self.dispatcher.twilio is None:
            return
        try:
            async with self.db.session() as session:
                repo = PriceRepository(session)
                last_call_at = await repo.get_last_twilio_call_at()
                self.dispatcher.twilio.seed_last_call_at(last_call_at)
                if last_call_at:
                    logger.info("Hydrated Twilio last call timestamp from DB: %s", last_call_at)
        except Exception:
            logger.warning("Could not hydrate Twilio last call timestamp from DB", exc_info=True)

    async def process_scraped_product(
        self,
        scraped: ScrapedProduct,
        *,
        retailer_base_url: str,
        stats: CycleStats,
    ) -> None:
        """Persist a scraped product, evaluate anomalies, and dispatch alerts."""
        async with self.db.session() as session:
            repo = PriceRepository(session)
            retailer = await repo.get_or_create_retailer(
                name=scraped.retailer_name,
                base_url=retailer_base_url,
            )
            product = await repo.upsert_product(
                retailer_id=retailer.id,
                sku=scraped.sku,
                name=scraped.name,
                category=scraped.category,
                url=scraped.url,
            )

            # Query historical context BEFORE recording the new observation
            context = await repo.get_price_context(
                product.id,
                window_days=self.settings.history_window_days,
            )
            recent_db_alerts = await repo.get_recent_alerts(
                product.id,
                cooldown_hours=self.settings.alert_cooldown_hours,
            )

            if scraped.parse_ok and scraped.price_clp is not None and scraped.price_clp >= 0:
                await repo.record_price(
                    product_id=product.id,
                    price=scraped.price_clp,
                    previous_price=context.previous_price,
                    normal_price=scraped.normal_price_clp,
                    payment_method=scraped.payment_method.value,
                    payment_method_label=scraped.payment_method_label,
                    in_stock=scraped.in_stock,
                )
                stats.prices_recorded += 1

            observation = PriceObservation(
                current_price=scraped.price_clp,
                in_stock=scraped.in_stock,
                normal_price=scraped.normal_price_clp,
                parse_ok=scraped.parse_ok,
                payment_method=scraped.payment_method,
                payment_method_label=scraped.payment_method_label,
            )
            recent_alerts = [
                AlertHistoryEntry(
                    alert_type=a.alert_type,
                    price_triggered=a.price_triggered,
                    sent_at=a.sent_at,
                )
                for a in recent_db_alerts
            ]

            decision = self.detector.evaluate(
                observation,
                history_30d=context.history_prices,
                previous_price=context.previous_price,
                recent_alerts=recent_alerts,
            )

            if not decision.should_alert or decision.alert_type is None or scraped.price_clp is None:
                return

            payload = AlertPayload(
                retailer_name=scraped.retailer_name,
                product_name=scraped.name,
                sku=scraped.sku,
                url=scraped.url,
                category=scraped.category,
                alert_type=decision.alert_type,
                current_price=scraped.price_clp,
                reference_price=decision.reference_price,
                reference_label=_reference_label_for_reason(decision.reason),
                discount_pct=decision.effective_discount_pct,
                payment_method=scraped.payment_method,
                payment_method_label=scraped.payment_method_label,
                all_payment_price_clp=scraped.all_payment_price_clp,
                avg_30d_price=int(decision.avg_30d) if decision.avg_30d is not None else None,
                min_30d_price=decision.min_30d,
                drop_vs_avg_pct=decision.drop_vs_avg_pct,
                history_samples=decision.history_samples,
                product_id=product.id,
                is_cold_start=decision.is_cold_start,
                reason=decision.reason,
            )

            dispatch_result = await self.dispatcher.dispatch(payload)
            if dispatch_result.any_sent:
                await repo.record_alert(
                    product_id=product.id,
                    alert_type=decision.alert_type.value,
                    price_triggered=scraped.price_clp,
                    twilio_called=dispatch_result.twilio_called,
                )
                stats.alerts_triggered += 1
                if dispatch_result.twilio_called:
                    stats.twilio_calls += 1

    async def run_watchlist_cycle(self) -> CycleStats:
        """Run high-frequency watchlist monitoring across all enabled stores."""
        stats = CycleStats()
        targets = load_targets_config(self.settings.targets_config_path)

        for store_cfg in targets:
            if not store_cfg.enabled or not store_cfg.watchlist:
                continue

            async with create_scraper(store_cfg.slug, self.settings) as scraper:
                async def _scrape_one(target: WatchlistTarget, slug: str = store_cfg.slug) -> None:
                    async with self._semaphore:
                        try:
                            scraped = await scraper.scrape_product(
                                target.url, category_name=target.category
                            )
                            if scraped is None:
                                return
                            stats.products_scraped += 1
                            await self.process_scraped_product(
                                scraped,
                                retailer_base_url=scraper.base_url,
                                stats=stats,
                            )
                        except Exception:
                            stats.errors += 1
                            logger.exception("[%s] Watchlist error on %s", slug, target.url)

                await asyncio.gather(*(_scrape_one(t) for t in store_cfg.watchlist))

        logger.info(
            "Watchlist cycle finished: scraped=%d recorded=%d alerts=%d twilio_calls=%d errors=%d",
            stats.products_scraped,
            stats.prices_recorded,
            stats.alerts_triggered,
            stats.twilio_calls,
            stats.errors,
        )
        return stats

    async def process_scraped_batch(
        self,
        products: list[ScrapedProduct],
        *,
        retailer_base_url: str,
        stats: CycleStats,
    ) -> None:
        """Persist a full category batch in ~4 SQL round-trips, evaluate in memory, and dispatch alerts."""
        if not products:
            return

        from datetime import UTC, datetime
        from decimal import Decimal

        # Deduplicate by SKU keeping the last observation
        deduped: dict[str, ScrapedProduct] = {}
        for p in products:
            deduped[p.sku] = p
        batch = list(deduped.values())
        stats.products_scraped += len(batch)

        retailer_name = batch[0].retailer_name
        pending_alerts: list[tuple[int, AlertPayload, str, int]] = []

        async with self._db_semaphore:
            async with self.db.session() as session:
                repo = PriceRepository(session)
                retailer = await repo.get_or_create_retailer(
                    name=retailer_name,
                    base_url=retailer_base_url,
                )
                sku_to_id = await repo.upsert_products_batch(
                    retailer_id=retailer.id,
                    items=[
                        {
                            "sku": p.sku,
                            "name": p.name,
                            "category": p.category,
                            "url": p.url,
                        }
                        for p in batch
                    ],
                )
                product_ids = list(sku_to_id.values())
                stats_map = await repo.get_price_stats_batch(
                    product_ids,
                    window_days=self.settings.history_window_days,
                )
                recent_alerts_map = await repo.get_recent_alerts_batch(
                    product_ids,
                    cooldown_hours=self.settings.alert_cooldown_hours,
                )

                from datetime import timedelta

                now = datetime.now(UTC)
                min_interval = timedelta(minutes=self.settings.price_record_min_interval_minutes)
                price_rows: list[dict[str, object]] = []
                for scraped in batch:
                    pid = sku_to_id.get(scraped.sku)
                    if pid is None:
                        continue

                    median_30d, avg_30d, min_30d, samples, previous_price, last_scraped_at = (
                        stats_map.get(pid, (None, None, None, 0, None, None))
                    )

                    if scraped.parse_ok and scraped.price_clp is not None and scraped.price_clp >= 0:
                        price_changed = previous_price is None or scraped.price_clp != previous_price
                        interval_elapsed = (
                            last_scraped_at is None
                            or (now - last_scraped_at) >= min_interval
                        )
                        if price_changed or interval_elapsed:
                            price_rows.append(
                                {
                                    "product_id": pid,
                                    "price": scraped.price_clp,
                                    "previous_price": previous_price,
                                    "normal_price": scraped.normal_price_clp,
                                    "payment_method": scraped.payment_method.value,
                                    "payment_method_label": scraped.payment_method_label,
                                    "in_stock": scraped.in_stock,
                                    "scraped_at": now,
                                }
                            )

                    observation = PriceObservation(
                        current_price=scraped.price_clp,
                        in_stock=scraped.in_stock,
                        normal_price=scraped.normal_price_clp,
                        parse_ok=scraped.parse_ok,
                        payment_method=scraped.payment_method,
                        payment_method_label=scraped.payment_method_label,
                    )
                    recent_alerts = [
                        AlertHistoryEntry(
                            alert_type=a.alert_type,
                            price_triggered=a.price_triggered,
                            sent_at=a.sent_at,
                        )
                        for a in recent_alerts_map.get(pid, [])
                    ]
                    decision = self.detector.evaluate(
                        observation,
                        previous_price=previous_price,
                        recent_alerts=recent_alerts,
                        precomputed_median_30d=median_30d if isinstance(median_30d, Decimal) else None,
                        precomputed_avg_30d=avg_30d if isinstance(avg_30d, Decimal) else None,
                        precomputed_min_30d=min_30d,
                        precomputed_samples=samples,
                    )
                    if (
                        decision.should_alert
                        and decision.alert_type is not None
                        and scraped.price_clp is not None
                    ):
                        payload = AlertPayload(
                            retailer_name=scraped.retailer_name,
                            product_name=scraped.name,
                            sku=scraped.sku,
                            url=scraped.url,
                            category=scraped.category,
                            alert_type=decision.alert_type,
                            current_price=scraped.price_clp,
                            reference_price=decision.reference_price,
                            reference_label=_reference_label_for_reason(decision.reason),
                            discount_pct=decision.effective_discount_pct,
                            payment_method=scraped.payment_method,
                            payment_method_label=scraped.payment_method_label,
                            all_payment_price_clp=scraped.all_payment_price_clp,
                            avg_30d_price=int(decision.avg_30d) if decision.avg_30d is not None else None,
                            min_30d_price=decision.min_30d,
                            drop_vs_avg_pct=decision.drop_vs_avg_pct,
                            history_samples=decision.history_samples,
                            product_id=pid,
                            is_cold_start=decision.is_cold_start,
                            reason=decision.reason,
                        )
                        pending_alerts.append(
                            (pid, payload, decision.alert_type.value, scraped.price_clp)
                        )

                inserted = await repo.record_prices_batch(price_rows)
                stats.prices_recorded += inserted

        # Dispatch notifications outside the DB transaction
        for pid, payload, alert_type_str, price_triggered in pending_alerts:
            try:
                dispatch_result = await self.dispatcher.dispatch(payload)
                if dispatch_result.any_sent:
                    async with self._db_semaphore:
                        async with self.db.session() as session:
                            repo = PriceRepository(session)
                            await repo.record_alert(
                                product_id=pid,
                                alert_type=alert_type_str,
                                price_triggered=price_triggered,
                                twilio_called=dispatch_result.twilio_called,
                            )
                    stats.alerts_triggered += 1
                    if dispatch_result.twilio_called:
                        stats.twilio_calls += 1
            except Exception:
                stats.errors += 1
                logger.exception("[%s] Alert dispatch failed for SKU %s", retailer_name, payload.sku)

    async def run_category_cycle(self) -> CycleStats:
        """Run category discovery monitoring across all enabled stores in parallel."""
        stats = CycleStats()
        targets = [
            cfg
            for cfg in load_targets_config(self.settings.targets_config_path)
            if cfg.enabled and cfg.categories
        ]

        async def _run_store(store_cfg: StoreTargetConfig) -> None:
            async with create_scraper(store_cfg.slug, self.settings) as scraper:
                for cat in store_cfg.categories:
                    try:
                        async with self._semaphore:
                            products = await scraper.scrape_category(
                                cat.url,
                                category_name=cat.name,
                                max_pages=cat.max_pages,
                            )
                        await self.process_scraped_batch(
                            products,
                            retailer_base_url=scraper.base_url,
                            stats=stats,
                        )
                    except Exception:
                        stats.errors += 1
                        logger.exception(
                            "[%s] Category scrape failed for '%s'",
                            store_cfg.slug,
                            cat.name,
                        )

        await asyncio.gather(*(_run_store(cfg) for cfg in targets))

        logger.info(
            "Category cycle finished: scraped=%d recorded=%d alerts=%d twilio_calls=%d errors=%d",
            stats.products_scraped,
            stats.prices_recorded,
            stats.alerts_triggered,
            stats.twilio_calls,
            stats.errors,
        )
        return stats

    async def prune_old_history(self) -> int:
        """Delete price_history rows older than `history_retention_days`."""
        try:
            async with self.db.session() as session:
                repo = PriceRepository(session)
                deleted = await repo.prune_old_history(
                    retention_days=self.settings.history_retention_days
                )
            if deleted > 0:
                logger.info(
                    "Pruned %d old price_history rows (retention=%dd)",
                    deleted,
                    self.settings.history_retention_days,
                )
            return deleted
        except Exception:
            logger.warning("Could not prune old price_history rows", exc_info=True)
            return 0
