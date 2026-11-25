from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)

_PRELOADED_STATE_RE = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>",
    re.DOTALL,
)


def _parse_xiaomi_price(raw: Any) -> int | None:
    """Parse Xiaomi Chile prices like '249990.00' (decimal dot, NOT CLP thousands)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit() and "." in text and len(text.split(".")[1]) <= 2:
        try:
            val = int(Decimal(text))
            return val if val >= 0 else None
        except InvalidOperation:
            pass
    return parse_clp_price(text)


def _with_xiaomi_page_param(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlsplit(url)
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query_items.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def _extract_preloaded_state(html: str) -> dict[str, Any] | None:
    match = _PRELOADED_STATE_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        logger.warning("Failed to decode Xiaomi __PRELOADED_STATE__ JSON")
        return None


class XiaomiScraper(BaseScraper):
    """Production scraper for Xiaomi Chile (https://www.mi.com/cl).

    Extracts catalog items directly from SSR `window.__PRELOADED_STATE__`
    (`pagedata.product_list.dataProvider.data`), capturing sale price, market
    (normal) price, and stock flags (`has_store` & `is_sale`).
    """

    retailer_name = "Xiaomi"
    retailer_slug = "xiaomi"
    base_url = "https://www.mi.com/cl"

    def parse_category_html(
        self,
        html: str,
        *,
        category_name: str,
    ) -> list[ScrapedProduct]:
        state = _extract_preloaded_state(html)
        if not state:
            return []

        pagedata = state.get("pagedata") or {}
        product_list = pagedata.get("product_list") or {}
        data_provider = product_list.get("dataProvider") or {}
        entries = data_provider.get("data") or []
        if not isinstance(entries, list):
            return []

        products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parent = entry.get("product") or {}
            commodities = entry.get("commodity") or []
            if not isinstance(commodities, list):
                continue

            for item in commodities:
                if not isinstance(item, dict):
                    continue
                sku = str(item.get("id") or parent.get("id") or "").strip()
                name = str(item.get("name") or item.get("short_name") or parent.get("name") or "").strip()
                if not sku or not name or sku in seen_skus:
                    continue

                price = _parse_xiaomi_price(item.get("price_min") or item.get("price_max"))
                normal_price = _parse_xiaomi_price(
                    item.get("market_price_max") or item.get("market_price_min")
                )
                if normal_price is not None and price is not None and normal_price <= price:
                    normal_price = None

                has_store = item.get("has_store", 1)
                is_sale = item.get("is_sale", 1)
                in_stock = bool(int(has_store)) and bool(int(is_sale))

                raw_link = str(item.get("item_link") or "").strip()
                product_url = urljoin(self.base_url, raw_link) if raw_link else self.base_url

                seen_skus.add(sku)
                products.append(
                    ScrapedProduct(
                        retailer_name=self.retailer_name,
                        sku=sku,
                        name=name,
                        category=category_name,
                        url=product_url,
                        price_clp=price,
                        normal_price_clp=normal_price,
                        all_payment_price_clp=price,
                        card_price_clp=None,
                        payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
                        payment_method_label="Todo medio de pago",
                        in_stock=in_stock,
                        parse_ok=price is not None,
                    )
                )

        return products

    def parse_product_html(
        self,
        html: str,
        *,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        parsed = self.parse_category_html(html, category_name=category_name or "General")
        return parsed[0] if parsed else None

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        all_products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for page in range(1, max(1, max_pages) + 1):
            page_url = _with_xiaomi_page_param(category_url, page)
            html = await self.fetch_hybrid(page_url)
            parsed = self.parse_category_html(html, category_name=category_name)

            if not parsed and self.settings.playwright_fallback_enabled:
                logger.info(
                    "[Xiaomi] Empty HTTP parse for %s (page %d); trying Playwright.",
                    category_name,
                    page,
                )
                html = await self.fetch_browser(page_url)
                parsed = self.parse_category_html(html, category_name=category_name)

            if not parsed:
                break

            new_count = 0
            for item in parsed:
                if item.sku not in seen_skus:
                    seen_skus.add(item.sku)
                    all_products.append(item)
                    new_count += 1

            if new_count == 0:
                break

        logger.info(
            "[Xiaomi] Scraped %d products from category '%s'",
            len(all_products),
            category_name,
        )
        return all_products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        html = await self.fetch_hybrid(product_url)
        return self.parse_product_html(
            html,
            product_url=product_url,
            category_name=category_name,
        )
