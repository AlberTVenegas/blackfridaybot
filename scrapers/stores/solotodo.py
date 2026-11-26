from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)

CLP_CURRENCY_ID = 1


def _parse_solotodo_price(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        try:
            val = int(Decimal(text))
            return val if val >= 0 else None
        except InvalidOperation:
            pass
    return parse_clp_price(text)


def _with_solotodo_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    query_items = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"
    ]
    query_items.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), ""))


class SoloTodoScraper(BaseScraper):
    """Production scraper for SoloTodo Chile (https://www.solotodo.cl).

    Uses SoloTodo's public JSON browse API (`https://publicapi.solotodo.com/categories/<id>/browse/`)
    which aggregates real-time lowest prices across major Chilean tech & department stores
    (Ripley, Lider, PCFactory, SPDigital, Falabella, Paris, Entel, etc.).
    """

    retailer_name = "SoloTodo"
    retailer_slug = "solotodo"
    base_url = "https://www.solotodo.cl"

    def parse_browse_json(
        self,
        raw_json: str,
        *,
        category_name: str,
    ) -> tuple[list[ScrapedProduct], bool]:
        """Parse SoloTodo browse API response. Returns (products, has_next_page)."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("[SoloTodo] Failed to decode browse JSON")
            return [], False

        if not isinstance(data, dict):
            return [], False

        results = data.get("results") or []
        has_next = bool(data.get("next"))
        if not isinstance(results, list):
            return [], has_next

        products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for bucket in results:
            if not isinstance(bucket, dict):
                continue
            entries = bucket.get("product_entries") or []
            if not isinstance(entries, list):
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                prod = entry.get("product") or {}
                meta = entry.get("metadata") or {}
                prod_id = prod.get("id")
                name = str(prod.get("name") or "").strip()
                if not prod_id or not name:
                    continue

                sku = str(prod_id)
                if sku in seen_skus:
                    continue

                prices_list = meta.get("prices_per_currency") or []
                clp_entry = next(
                    (
                        p
                        for p in prices_list
                        if isinstance(p, dict)
                        and (p.get("currency_id") == CLP_CURRENCY_ID or p.get("currency") == CLP_CURRENCY_ID)
                    ),
                    None,
                )
                if not clp_entry:
                    continue

                offer_price = _parse_solotodo_price(clp_entry.get("offer_price"))
                normal_price = _parse_solotodo_price(clp_entry.get("normal_price"))

                if offer_price is None and normal_price is None:
                    continue

                if (
                    self.settings.include_store_card_prices
                    and offer_price is not None
                    and (normal_price is None or offer_price < normal_price)
                ):
                    selected_price = offer_price
                    payment_method = PaymentMethodType.STORE_CARD
                    payment_label = "Precio oferta tienda / transferencia"
                    card_price = offer_price
                else:
                    selected_price = normal_price if normal_price is not None else offer_price
                    payment_method = PaymentMethodType.ALL_PAYMENT_METHODS
                    payment_label = "Todo medio de pago"
                    card_price = offer_price if (offer_price and normal_price and offer_price < normal_price) else None

                ref_normal = (
                    normal_price
                    if (normal_price is not None and selected_price is not None and normal_price > selected_price)
                    else None
                )

                seen_skus.add(sku)
                products.append(
                    ScrapedProduct(
                        retailer_name=self.retailer_name,
                        sku=sku,
                        name=name,
                        category=category_name,
                        url=f"{self.base_url}/products/{sku}",
                        price_clp=selected_price,
                        normal_price_clp=ref_normal,
                        all_payment_price_clp=normal_price or selected_price,
                        card_price_clp=card_price,
                        payment_method=payment_method,
                        payment_method_label=payment_label,
                        in_stock=True,
                        parse_ok=selected_price is not None,
                    )
                )

        return products, has_next

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        all_products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for page in range(1, max(1, max_pages) + 1):
            page_url = _with_solotodo_page(category_url, page)
            raw = await self.fetch_http(
                page_url,
                extra_headers={"Accept": "application/json"},
            )
            parsed, has_next = self.parse_browse_json(raw, category_name=category_name)
            if not parsed:
                break

            for item in parsed:
                if item.sku not in seen_skus:
                    seen_skus.add(item.sku)
                    all_products.append(item)

            if not has_next:
                break

        logger.info(
            "[SoloTodo] Scraped %d products from category '%s'",
            len(all_products),
            category_name,
        )
        return all_products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        # Extract numeric product ID from https://www.solotodo.cl/products/264814-...
        path = urlsplit(product_url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            return None
        pid = parts[-1].split("-")[0]
        if not pid.isdigit():
            return None

        api_url = f"https://publicapi.solotodo.com/products/{pid}/"
        raw = await self.fetch_http(api_url, extra_headers={"Accept": "application/json"})
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        name = str(data.get("name") or "").strip()
        if not name:
            return None

        # Fetch active entities for lowest CLP price
        ent_raw = await self.fetch_http(
            f"https://publicapi.solotodo.com/products/{pid}/entities/",
            extra_headers={"Accept": "application/json"},
        )
        try:
            entities = json.loads(ent_raw)
        except json.JSONDecodeError:
            entities = []

        offer_prices: list[int] = []
        normal_prices: list[int] = []
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                reg = ent.get("active_registry")
                if not isinstance(reg, dict) or not reg.get("is_available"):
                    continue
                op = _parse_solotodo_price(reg.get("offer_price"))
                np = _parse_solotodo_price(reg.get("normal_price"))
                if op and op > 0:
                    offer_prices.append(op)
                if np and np > 0:
                    normal_prices.append(np)

        best_offer = min(offer_prices) if offer_prices else None
        best_normal = min(normal_prices) if normal_prices else None
        in_stock = bool(best_offer or best_normal)
        selected = best_offer if (self.settings.include_store_card_prices and best_offer) else (best_normal or best_offer)

        return ScrapedProduct(
            retailer_name=self.retailer_name,
            sku=pid,
            name=name,
            category=category_name or "Tecnología",
            url=f"{self.base_url}/products/{pid}",
            price_clp=selected,
            normal_price_clp=best_normal if (best_normal and selected and best_normal > selected) else None,
            all_payment_price_clp=best_normal or selected,
            card_price_clp=best_offer if (best_offer and best_normal and best_offer < best_normal) else None,
            payment_method=(
                PaymentMethodType.STORE_CARD
                if (best_offer and best_normal and best_offer < best_normal and self.settings.include_store_card_prices)
                else PaymentMethodType.ALL_PAYMENT_METHODS
            ),
            payment_method_label=(
                "Precio oferta tienda / transferencia"
                if (best_offer and best_normal and best_offer < best_normal and self.settings.include_store_card_prices)
                else "Todo medio de pago"
            ),
            in_stock=in_stock,
            parse_ok=selected is not None,
        )
