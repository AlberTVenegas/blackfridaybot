from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)


def _parse_shopify_price(raw: Any) -> int | None:
    """Parse Shopify CLP prices like '53990' or '53990.00'."""
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


def _to_shopify_json_url(url: str, *, page: int = 1, limit: int = 250) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith(".json"):
        path = f"{path}/products.json" if path else "/products.json"
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("page", "limit")
    ]
    query_items.append(("limit", str(limit)))
    query_items.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query_items), ""))


class AlishaPerfumesScraper(BaseScraper):
    """Production scraper for Alisha Perfumes Chile (https://alishaperfumes.cl).

    Uses Shopify's native JSON catalog endpoint (`/products.json?limit=250&page=N`)
    to fetch up to 250 perfumes per request with exact sale price, compare-at
    (normal) price, and variant availability.
    """

    retailer_name = "Alisha Perfumes"
    retailer_slug = "alishaperfumes"
    base_url = "https://alishaperfumes.cl"

    def _parse_shopify_product(
        self,
        item: dict[str, Any],
        *,
        category_name: str | None,
    ) -> ScrapedProduct | None:
        title = str(item.get("title") or "").strip()
        handle = str(item.get("handle") or "").strip()
        prod_id = str(item.get("id") or "").strip()
        if not title or not (handle or prod_id):
            return None

        variants = item.get("variants") if isinstance(item.get("variants"), list) else []
        if not variants:
            return None

        # Prefer lowest-priced in-stock variant; fallback to first variant if all out of stock
        available_variants: list[tuple[int, dict[str, Any]]] = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            p = _parse_shopify_price(v.get("price"))
            if p is not None and p > 0 and bool(v.get("available", True)):
                available_variants.append((p, v))

        if available_variants:
            available_variants.sort(key=lambda pair: pair[0])
            best_price, chosen = available_variants[0]
            in_stock = True
        else:
            chosen = next((v for v in variants if isinstance(v, dict)), {})
            best_price = _parse_shopify_price(chosen.get("price"))
            in_stock = False

        normal_price = _parse_shopify_price(chosen.get("compare_at_price"))
        if normal_price is not None and best_price is not None and normal_price <= best_price:
            normal_price = None

        raw_sku = str(chosen.get("sku") or "").strip()
        sku = raw_sku or str(chosen.get("id") or prod_id)
        product_url = (
            urljoin(self.base_url, f"/products/{handle}") if handle else self.base_url
        )

        return ScrapedProduct(
            retailer_name=self.retailer_name,
            sku=sku,
            name=title,
            category=category_name or str(item.get("product_type") or "Perfumes").strip() or "Perfumes",
            url=product_url,
            price_clp=best_price,
            normal_price_clp=normal_price,
            all_payment_price_clp=best_price,
            card_price_clp=None,
            payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
            payment_method_label="Todo medio de pago",
            in_stock=in_stock,
            parse_ok=best_price is not None,
        )

    def parse_products_json(
        self,
        raw_json: str,
        *,
        category_name: str,
    ) -> list[ScrapedProduct]:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("[Alisha Perfumes] Failed to decode Shopify JSON payload")
            return []

        entries = data.get("products") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []

        products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parsed = self._parse_shopify_product(entry, category_name=category_name)
            if parsed and parsed.sku not in seen_skus:
                seen_skus.add(parsed.sku)
                products.append(parsed)
        return products

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 4,
    ) -> list[ScrapedProduct]:
        all_products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for page in range(1, max(1, max_pages) + 1):
            page_url = _to_shopify_json_url(category_url, page=page, limit=250)
            raw = await self.fetch_http(
                page_url,
                extra_headers={"Accept": "application/json"},
            )
            parsed = self.parse_products_json(raw, category_name=category_name)
            if not parsed:
                break

            new_count = 0
            for item in parsed:
                if item.sku not in seen_skus:
                    seen_skus.add(item.sku)
                    all_products.append(item)
                    new_count += 1

            if new_count == 0 or len(parsed) < 250:
                break

        logger.info(
            "[Alisha Perfumes] Scraped %d products from category '%s'",
            len(all_products),
            category_name,
        )
        return all_products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        parts = urlsplit(product_url)
        path = parts.path.rstrip("/")
        json_path = path if path.endswith(".json") else f"{path}.json"
        json_url = urlunsplit((parts.scheme, parts.netloc, json_path, "", ""))

        raw = await self.fetch_http(
            json_url,
            extra_headers={"Accept": "application/json"},
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        item = data.get("product") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return None
        return self._parse_shopify_product(item, category_name=category_name or "Perfumes")
