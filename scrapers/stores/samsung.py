from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)


def _parse_samsung_price(raw: Any) -> int | None:
    """Parse Samsung JSON-LD CLP prices (handles integers, strings, and '839990.00')."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    # Handle decimal dot format like "839990.00" safely before CLP dot-thousands parser
    if text.replace(".", "", 1).isdigit() and "." in text and len(text.split(".")[1]) <= 2:
        try:
            val = int(Decimal(text))
            return val if val >= 0 else None
        except InvalidOperation:
            pass
    return parse_clp_price(text)


def _sku_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    if not path:
        return ""
    return path.split("/")[-1][:120]


def _extract_json_ld_objects(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    objects: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            objects.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            objects.append(data)
    return objects


class SamsungScraper(BaseScraper):
    """Production scraper for Samsung Chile (https://www.samsung.com/cl).

    Extracts products directly from Samsung's SSR JSON-LD `ItemList` / `Product`
    payloads (smartphones, tablets, TVs) without requiring headless rendering.
    """

    retailer_name = "Samsung"
    retailer_slug = "samsung"
    base_url = "https://www.samsung.com/cl"

    def _parse_ld_product(
        self,
        item: dict[str, Any],
        *,
        category_name: str | None,
        fallback_url: str,
    ) -> ScrapedProduct | None:
        name = str(item.get("name") or "").strip()
        raw_url = str(item.get("url") or item.get("@id") or fallback_url).strip()
        product_url = urljoin(self.base_url, raw_url)
        sku = str(item.get("sku") or item.get("mpn") or _sku_from_url(product_url)).strip()
        if not name or not sku:
            return None

        offers = item.get("offers")
        if isinstance(offers, list):
            offer = offers[0] if offers and isinstance(offers[0], dict) else {}
        elif isinstance(offers, dict):
            offer = offers
        else:
            offer = {}

        price = _parse_samsung_price(offer.get("price") or offer.get("lowPrice"))
        normal_price = _parse_samsung_price(offer.get("highPrice"))
        if normal_price is not None and price is not None and normal_price <= price:
            normal_price = None

        availability = str(offer.get("availability", "")).lower()
        in_stock = "outofstock" not in availability and "soldout" not in availability

        return ScrapedProduct(
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

    def parse_category_html(
        self,
        html: str,
        *,
        category_name: str,
    ) -> list[ScrapedProduct]:
        products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for obj in _extract_json_ld_objects(html):
            obj_type = obj.get("@type")
            candidates: list[dict[str, Any]] = []
            if obj_type == "ItemList" and isinstance(obj.get("itemListElement"), list):
                for entry in obj["itemListElement"]:
                    if isinstance(entry, dict) and isinstance(entry.get("item"), dict):
                        candidates.append(entry["item"])
                    elif isinstance(entry, dict) and entry.get("@type") == "Product":
                        candidates.append(entry)
            elif obj_type == "Product":
                candidates.append(obj)

            for item in candidates:
                parsed = self._parse_ld_product(
                    item,
                    category_name=category_name,
                    fallback_url=self.base_url,
                )
                if parsed and parsed.sku not in seen_skus:
                    seen_skus.add(parsed.sku)
                    products.append(parsed)

        return products

    def parse_product_html(
        self,
        html: str,
        *,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        for obj in _extract_json_ld_objects(html):
            if obj.get("@type") == "Product":
                parsed = self._parse_ld_product(
                    obj,
                    category_name=category_name,
                    fallback_url=product_url,
                )
                if parsed:
                    return parsed
        return None

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 1,
    ) -> list[ScrapedProduct]:
        html = await self.fetch_hybrid(
            category_url,
            wait_selector='script[type="application/ld+json"]',
        )
        products = self.parse_category_html(html, category_name=category_name)
        if not products and self.settings.playwright_fallback_enabled:
            logger.info("[Samsung] Empty HTTP parse for %s; trying Playwright.", category_name)
            html = await self.fetch_browser(
                category_url,
                wait_selector='script[type="application/ld+json"]',
            )
            products = self.parse_category_html(html, category_name=category_name)

        logger.info(
            "[Samsung] Scraped %d products from category '%s'",
            len(products),
            category_name,
        )
        return products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        html = await self.fetch_hybrid(
            product_url,
            wait_selector='script[type="application/ld+json"]',
        )
        product = self.parse_product_html(
            html,
            product_url=product_url,
            category_name=category_name,
        )
        if product is None and self.settings.playwright_fallback_enabled:
            html = await self.fetch_browser(
                product_url,
                wait_selector='script[type="application/ld+json"]',
            )
            product = self.parse_product_html(
                html,
                product_url=product_url,
                category_name=category_name,
            )
        return product
