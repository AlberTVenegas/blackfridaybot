"""Phase 2 retailer scraper stubs.

Each stub inherits from BaseScraper and documents the recommended extraction strategy
for Chilean storefronts so Phase 2 can implement them incrementally without touching
the pipeline, database, anomaly detector, or notifier layers.
"""

from __future__ import annotations

from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct


class _Phase2StubScraper(BaseScraper):
    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        raise NotImplementedError(
            f"Scraper for '{self.retailer_name}' is scheduled for Phase 2."
        )

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        raise NotImplementedError(
            f"Scraper for '{self.retailer_name}' is scheduled for Phase 2."
        )


class RipleyScraper(_Phase2StubScraper):
    """Target: Ripley Chile — BFF JSON endpoints + Tarjeta Ripley vs Todo medio."""

    retailer_name = "Ripley"
    retailer_slug = "ripley"
    base_url = "https://simple.ripley.cl"


class SamsungScraper(_Phase2StubScraper):
    """Target: Samsung Chile — Hybris / catalog JSON APIs + Playwright fallback."""

    retailer_name = "Samsung"
    retailer_slug = "samsung"
    base_url = "https://www.samsung.com/cl"


class XiaomiScraper(_Phase2StubScraper):
    """Target: Xiaomi Chile (mi.com/cl) — VTEX/JSON catalog endpoints."""

    retailer_name = "Xiaomi"
    retailer_slug = "xiaomi"
    base_url = "https://www.mi.com/cl"


class NikeScraper(_Phase2StubScraper):
    """Target: Nike Chile — VTEX Intelligent Search API (`/api/io/_v/api/intelligent-search`)."""

    retailer_name = "Nike"
    retailer_slug = "nike"
    base_url = "https://www.nike.cl"


class AdidasScraper(_Phase2StubScraper):
    """Target: Adidas Chile — `/api/plp/content-engine` + Playwright stealth fallback."""

    retailer_name = "Adidas"
    retailer_slug = "adidas"
    base_url = "https://www.adidas.cl"


class DecathlonScraper(_Phase2StubScraper):
    """Target: Decathlon Chile — VTEX / Algolia JSON catalog endpoints."""

    retailer_name = "Decathlon"
    retailer_slug = "decathlon"
    base_url = "https://www.decathlon.cl"
