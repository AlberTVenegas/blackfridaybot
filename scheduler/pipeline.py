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
    return "Mediana 30 días"


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
        self._db_semaphore = asyncio.Semaphore(max(2, min(8, settings.db_pool_size + 3)))

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

    async def run_category_cycle(self) -> CycleStats:
        """Run category discovery monitoring across all enabled stores."""
        stats = CycleStats()
        targets = load_targets_config(self.settings.targets_config_path)

        for store_cfg in targets:
            if not store_cfg.enabled or not store_cfg.categories:
                continue

            async with create_scraper(store_cfg.slug, self.settings) as scraper:
                for cat in store_cfg.categories:
                    try:
                        async with self._semaphore:
                            products = await scraper.scrape_category(
                                cat.url,
                                category_name=cat.name,
                                max_pages=cat.max_pages,
                            )
                        async def _process_one(
                            scraped: ScrapedProduct,
                            slug: str = store_cfg.slug,
                            base_url: str = scraper.base_url,
                        ) -> None:
                            stats.products_scraped += 1
                            async with self._db_semaphore:
                                try:
                                    await self.process_scraped_product(
                                        scraped,
                                        retailer_base_url=base_url,
                                        stats=stats,
                                    )
                                except Exception:
                                    stats.errors += 1
                                    logger.exception(
                                        "[%s] Failed processing SKU %s",
                                        slug,
                                        scraped.sku,
                                    )

                        await asyncio.gather(*(_process_one(p) for p in products))
                    except Exception:
                        stats.errors += 1
                        logger.exception(
                            "[%s] Category scrape failed for '%s'",
                            store_cfg.slug,
                            cat.name,
                        )

        logger.info(
            "Category cycle finished: scraped=%d recorded=%d alerts=%d twilio_calls=%d errors=%d",
            stats.products_scraped,
            stats.prices_recorded,
            stats.alerts_triggered,
            stats.twilio_calls,
            stats.errors,
        )
        return stats
