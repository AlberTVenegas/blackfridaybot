from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)


def _with_page_param(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _extract_next_data(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None
    try:
        return json.loads(script.string)
    except json.JSONDecodeError:
        logger.warning("Failed to decode Falabella __NEXT_DATA__ JSON")
        return None


def _extract_json_ld_products(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            if item_type == "Product":
                products.append(item)
            elif item_type == "ItemList" and isinstance(item.get("itemListElement"), list):
                for entry in item["itemListElement"]:
                    sub = entry.get("item") if isinstance(entry, dict) else None
                    if isinstance(sub, dict) and sub.get("@type") == "Product":
                        products.append(sub)
    return products


class FalabellaScraper(BaseScraper):
    """Hybrid scraper for Falabella Chile (categories + watchlist PDPs).

    Extraction priority:
      1. Fast curl_cffi HTTP request -> parse Next.js `__NEXT_DATA__` JSON.
      2. JSON-LD fallback if `__NEXT_DATA__` is absent.
      3. Automatic Playwright fallback if WAF blocks HTTP or JS rendering is required.

    Payment method distinction:
      - `cmrPrice` -> PaymentMethodType.STORE_CARD ("Exclusivo Tarjeta CMR")
      - `eventPrice` / `internetPrice` / `normalPrice` -> ALL_PAYMENT_METHODS ("Todo medio de pago")
      - `normalPrice` is also preserved as `normal_price_clp` for cold-start detection.
    """

    retailer_name = "Falabella"
    retailer_slug = "falabella"
    base_url = "https://www.falabella.com/falabella-cl"

    def _resolve_prices(
        self, price_entries: list[dict[str, Any]]
    ) -> tuple[int | None, int | None, int | None, int | None, PaymentMethodType, str]:
        """Parse Falabella's price list into best price, normal price, and payment method.

        Returns:
            (selected_price, normal_price, all_payment_price, card_price, payment_method, label)
        """
        card_candidates: list[int] = []
        all_payment_candidates: list[int] = []
        normal_candidates: list[int] = []

        for entry in price_entries:
            if not isinstance(entry, dict):
                continue
            price_type = str(entry.get("type", "")).lower()
            raw_values = entry.get("price")
            if isinstance(raw_values, list):
                parsed = next(
                    (p for val in raw_values if (p := parse_clp_price(val)) is not None),
                    None,
                )
            else:
                parsed = parse_clp_price(raw_values)

            if parsed is None:
                continue

            if "cmr" in price_type or "card" in price_type:
                card_candidates.append(parsed)
            elif "normal" in price_type or "list" in price_type:
                normal_candidates.append(parsed)
                if not entry.get("crossed", False):
                    all_payment_candidates.append(parsed)
            else:
                # eventPrice, internetPrice, promoPrice -> valid for all payment methods
                all_payment_candidates.append(parsed)

        normal_price = max(normal_candidates) if normal_candidates else None
        all_payment_price = min(all_payment_candidates) if all_payment_candidates else normal_price
        card_price = min(card_candidates) if card_candidates else None

        # Decide which price to report & whether it is store-card exclusive
        if (
            self.settings.include_store_card_prices
            and card_price is not None
            and (all_payment_price is None or card_price < all_payment_price)
        ):
            return (
                card_price,
                normal_price,
                all_payment_price,
                card_price,
                PaymentMethodType.STORE_CARD,
                "Exclusivo Tarjeta CMR",
            )

        if all_payment_price is not None:
            return (
                all_payment_price,
                normal_price,
                all_payment_price,
                card_price,
                PaymentMethodType.ALL_PAYMENT_METHODS,
                "Todo medio de pago",
            )

        return (
            None,
            normal_price,
            None,
            card_price,
            PaymentMethodType.ALL_PAYMENT_METHODS,
            "Todo medio de pago",
        )

    @staticmethod
    def _resolve_stock(item: dict[str, Any]) -> bool:
        """Determine product stock status from Falabella JSON payloads."""
        if item.get("isOutOfStock") is True or item.get("outOfStock") is True:
            return False
        if item.get("available") is False or item.get("inStock") is False:
            return False

        availability = item.get("availability")
        if isinstance(availability, dict):
            # e.g. {"homeDeliveryShipping": "", "pickUpFromStoreShipping": ""}
            if availability.get("isOutOfStock") is True:
                return False
            if availability.get("inStock") is False:
                return False
            if "homeDeliveryShipping" in availability or "pickUpFromStoreShipping" in availability:
                has_channel = any(
                    bool(availability.get(k))
                    for k in ("homeDeliveryShipping", "pickUpFromStoreShipping", "internationalShipping")
                )
                return has_channel
        elif isinstance(availability, str):
            lowered = availability.lower()
            if "outofstock" in lowered or "soldout" in lowered:
                return False
            if "instock" in lowered:
                return True

        # Check variants if PDP productData
        variants = item.get("variants")
        if isinstance(variants, list) and variants:
            any_in_stock = False
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                if variant.get("isOutOfStock") is True:
                    continue
                v_avail = variant.get("availability")
                if isinstance(v_avail, dict):
                    if any(
                        bool(v_avail.get(k))
                        for k in ("homeDeliveryShipping", "pickUpFromStoreShipping")
                    ):
                        any_in_stock = True
                        break
                else:
                    any_in_stock = True
                    break
            return any_in_stock

        return True

    def parse_category_html(
        self,
        html: str,
        *,
        category_name: str,
    ) -> list[ScrapedProduct]:
        """Parse a Falabella category listing HTML page into normalized ScrapedProducts."""
        next_data = _extract_next_data(html)
        results: list[dict[str, Any]] = []

        if next_data:
            page_props = next_data.get("props", {}).get("pageProps", {})
            raw_results = (
                page_props.get("results")
                or page_props.get("initialData", {}).get("results")
                or page_props.get("searchData", {}).get("results")
                or []
            )
            if isinstance(raw_results, list):
                results = raw_results

        products: list[ScrapedProduct] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("skuId") or item.get("productId") or "").strip()
            name = str(item.get("displayName") or item.get("name") or "").strip()
            raw_url = str(item.get("url") or "").strip()
            if not sku or not name:
                continue

            full_url = urljoin("https://www.falabella.com", raw_url) if raw_url else (
                f"{self.base_url}/product/{sku}"
            )
            prices = item.get("prices") if isinstance(item.get("prices"), list) else []
            (
                selected_price,
                normal_price,
                all_payment_price,
                card_price,
                payment_method,
                payment_label,
            ) = self._resolve_prices(prices)

            products.append(
                ScrapedProduct(
                    retailer_name=self.retailer_name,
                    sku=sku,
                    name=name,
                    url=full_url,
                    category=category_name,
                    price_clp=selected_price,
                    normal_price_clp=normal_price,
                    all_payment_price_clp=all_payment_price,
                    card_price_clp=card_price,
                    payment_method=payment_method,
                    payment_method_label=payment_label,
                    in_stock=self._resolve_stock(item),
                    parse_ok=selected_price is not None,
                )
            )

        if products:
            return products

        # Secondary fallback: JSON-LD product list
        for ld in _extract_json_ld_products(html):
            parsed = self._parse_json_ld_product(ld, fallback_url=self.base_url, category_name=category_name)
            if parsed is not None:
                products.append(parsed)

        return products

    def parse_product_html(
        self,
        html: str,
        *,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        """Parse a single Falabella Product Detail Page (PDP) HTML."""
        next_data = _extract_next_data(html)
        if next_data:
            page_props = next_data.get("props", {}).get("pageProps", {})
            product_data = page_props.get("productData") or page_props.get("product")
            if isinstance(product_data, dict):
                sku = str(
                    product_data.get("id")
                    or product_data.get("skuId")
                    or product_data.get("productId")
                    or ""
                ).strip()
                name = str(
                    product_data.get("name") or product_data.get("displayName") or ""
                ).strip()

                # Gather prices from top-level or first active variant
                prices = product_data.get("prices")
                if not isinstance(prices, list) or not prices:
                    variants = product_data.get("variants")
                    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
                        prices = variants[0].get("prices", [])
                        if not sku:
                            sku = str(variants[0].get("id") or "").strip()

                if sku and name:
                    (
                        selected_price,
                        normal_price,
                        all_payment_price,
                        card_price,
                        payment_method,
                        payment_label,
                    ) = self._resolve_prices(prices if isinstance(prices, list) else [])

                    return ScrapedProduct(
                        retailer_name=self.retailer_name,
                        sku=sku,
                        name=name,
                        url=product_url,
                        category=category_name,
                        price_clp=selected_price,
                        normal_price_clp=normal_price,
                        all_payment_price_clp=all_payment_price,
                        card_price_clp=card_price,
                        payment_method=payment_method,
                        payment_method_label=payment_label,
                        in_stock=self._resolve_stock(product_data),
                        parse_ok=selected_price is not None,
                    )

        # Fallback: JSON-LD Product block
        for ld in _extract_json_ld_products(html):
            parsed = self._parse_json_ld_product(
                ld, fallback_url=product_url, category_name=category_name
            )
            if parsed is not None:
                return parsed

        return None

    def _parse_json_ld_product(
        self,
        ld: dict[str, Any],
        *,
        fallback_url: str,
        category_name: str | None,
    ) -> ScrapedProduct | None:
        sku = str(ld.get("sku") or ld.get("productID") or "").strip()
        name = str(ld.get("name") or "").strip()
        if not sku or not name:
            return None

        offers = ld.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if not isinstance(offers, dict):
            offers = {}

        price_clp = parse_clp_price(offers.get("price"))
        availability = str(offers.get("availability", ""))
        in_stock = "outofstock" not in availability.lower()
        url = str(offers.get("url") or ld.get("url") or fallback_url)

        return ScrapedProduct(
            retailer_name=self.retailer_name,
            sku=sku,
            name=name,
            url=url,
            category=category_name,
            price_clp=price_clp,
            normal_price_clp=None,
            all_payment_price_clp=price_clp,
            card_price_clp=None,
            payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
            payment_method_label="Todo medio de pago",
            in_stock=in_stock,
            parse_ok=price_clp is not None,
        )

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        all_products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for page in range(1, max_pages + 1):
            page_url = _with_page_param(category_url, page)
            html = await self.fetch_hybrid(page_url, wait_selector="script#__NEXT_DATA__")
            parsed = self.parse_category_html(html, category_name=category_name)

            if not parsed and self.settings.playwright_fallback_enabled:
                logger.info(
                    "[Falabella] Empty HTTP parse on %s; retrying with Playwright.", page_url
                )
                html = await self.fetch_browser(page_url, wait_selector="script#__NEXT_DATA__")
                parsed = self.parse_category_html(html, category_name=category_name)

            if not parsed:
                break

            for prod in parsed:
                if prod.sku not in seen_skus:
                    seen_skus.add(prod.sku)
                    all_products.append(prod)

        logger.info(
            "[Falabella] Scraped %d products from category '%s'",
            len(all_products),
            category_name,
        )
        return all_products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        html = await self.fetch_hybrid(product_url, wait_selector="script#__NEXT_DATA__")
        product = self.parse_product_html(
            html, product_url=product_url, category_name=category_name
        )
        if product is None and self.settings.playwright_fallback_enabled:
            logger.info(
                "[Falabella] Empty HTTP PDP parse on %s; retrying with Playwright.",
                product_url,
            )
            html = await self.fetch_browser(product_url, wait_selector="script#__NEXT_DATA__")
            product = self.parse_product_html(
                html, product_url=product_url, category_name=category_name
            )
        return product
