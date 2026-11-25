from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from analyzer.models import PaymentMethodType
from scrapers.base import BaseScraper
from scrapers.models import ScrapedProduct
from scrapers.utils import parse_clp_price

logger = logging.getLogger(__name__)

_CLP_TEXT_RE = re.compile(r"\$\s*\d{1,3}(?:\.\d{3})*(?:\s*-\s*\$\s*\d{1,3}(?:\.\d{3})*)?")
_SINGLE_CLP_RE = re.compile(r"\$\s*\d{1,3}(?:\.\d{3})+|\$\s*\d+")


def _with_paris_page_param(url: str, page: int) -> str:
    """Attach Paris Chile pagination parameter (`page=N`) cleanly."""
    if page <= 1:
        return url
    parts = urlsplit(url)
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    query_items.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def _extract_json_ld_products(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
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
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                products.append(item)
            elif item_type == "ItemList" and isinstance(item.get("itemListElement"), list):
                for entry in item["itemListElement"]:
                    if isinstance(entry, dict) and isinstance(entry.get("item"), dict):
                        products.append(entry["item"])
    return products


class ParisScraper(BaseScraper):
    """Production scraper for Paris Chile (https://www.paris.cl).

    Extraction strategy:
      1. Fast curl_cffi HTTP request -> parse Constructor.io product cards
         (`[data-cnstrc-item-id]`) embedded directly in SSR HTML.
      2. Distinguish Tarjeta Cencosud exclusive price (lower price inside card)
         from all-payment-methods price (`data-cnstrc-item-price`) and normal price.
      3. JSON-LD fallback for product detail pages (watchlist) and Playwright fallback
         if blocked or empty.
    """

    retailer_name = "Paris"
    retailer_slug = "paris"
    base_url = "https://www.paris.cl"

    def _resolve_card_prices(
        self,
        *,
        base_price_raw: str | None,
        card_text: str,
    ) -> tuple[int | None, int | None, int | None, int | None, PaymentMethodType, str]:
        """Resolve best price, normal price, all-payment price, and Cencosud card price."""
        all_payment_price = parse_clp_price(base_price_raw)
        extracted_prices: list[int] = []
        for match in _SINGLE_CLP_RE.findall(card_text):
            parsed = parse_clp_price(match)
            if parsed is not None and parsed > 0:
                extracted_prices.append(parsed)

        if all_payment_price is None and extracted_prices:
            all_payment_price = min(extracted_prices)

        if all_payment_price is None:
            return (
                None,
                None,
                None,
                None,
                PaymentMethodType.ALL_PAYMENT_METHODS,
                "Todo medio de pago",
            )

        lower_prices = [p for p in extracted_prices if p < all_payment_price]
        higher_prices = [p for p in extracted_prices if p > all_payment_price]

        card_price = min(lower_prices) if lower_prices else None
        normal_price = max(higher_prices) if higher_prices else None

        if self.settings.include_store_card_prices and card_price is not None:
            return (
                card_price,
                normal_price,
                all_payment_price,
                card_price,
                PaymentMethodType.STORE_CARD,
                "Exclusivo Tarjeta Cencosud",
            )

        return (
            all_payment_price,
            normal_price,
            all_payment_price,
            card_price,
            PaymentMethodType.ALL_PAYMENT_METHODS,
            "Todo medio de pago",
        )

    def parse_category_html(
        self,
        html: str,
        *,
        category_name: str,
    ) -> list[ScrapedProduct]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("[data-cnstrc-item-id]")
        products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for card in cards:
            sku = str(card.get("data-cnstrc-item-id") or "").strip()
            name = str(card.get("data-cnstrc-item-name") or "").strip()
            if not sku or not name or sku in seen_skus:
                continue

            link = card.find("a", href=True)
            href = str(link["href"]).strip() if link else ""
            product_url = urljoin(self.base_url, href) if href else self.base_url

            card_text = " ".join(card.stripped_strings)
            lowered = card_text.lower()
            in_stock = "agotado" not in lowered and "sin stock" not in lowered

            (
                selected_price,
                normal_price,
                all_payment_price,
                card_price,
                payment_method,
                payment_label,
            ) = self._resolve_card_prices(
                base_price_raw=card.get("data-cnstrc-item-price"),
                card_text=card_text,
            )

            seen_skus.add(sku)
            products.append(
                ScrapedProduct(
                    retailer_name=self.retailer_name,
                    sku=sku,
                    name=name,
                    category=category_name,
                    url=product_url,
                    price_clp=selected_price,
                    normal_price_clp=normal_price,
                    all_payment_price_clp=all_payment_price,
                    card_price_clp=card_price,
                    payment_method=payment_method,
                    payment_method_label=payment_label,
                    in_stock=in_stock,
                    parse_ok=selected_price is not None,
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
        # 1. Try JSON-LD Product block (standard on Paris PDPs)
        for ld in _extract_json_ld_products(html):
            sku = str(ld.get("sku") or ld.get("mpn") or "").strip()
            name = str(ld.get("name") or "").strip()
            if not sku or not name:
                continue

            offers = ld.get("offers")
            if isinstance(offers, list):
                offer = offers[0] if offers else {}
            elif isinstance(offers, dict):
                offer = offers
            else:
                offer = {}

            price = parse_clp_price(offer.get("price") or offer.get("lowPrice"))
            normal_price = parse_clp_price(offer.get("highPrice"))
            availability = str(offer.get("availability", "")).lower()
            in_stock = "outofstock" not in availability and "soldout" not in availability

            return ScrapedProduct(
                retailer_name=self.retailer_name,
                sku=sku,
                name=name,
                category=category_name,
                url=urljoin(self.base_url, str(ld.get("url") or product_url)),
                price_clp=price,
                normal_price_clp=normal_price if normal_price != price else None,
                all_payment_price_clp=price,
                card_price_clp=None,
                payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
                payment_method_label="Todo medio de pago",
                in_stock=in_stock,
                parse_ok=price is not None,
            )

        # 2. Fallback: parse first Constructor card if present
        parsed_cards = self.parse_category_html(html, category_name=category_name or "General")
        if parsed_cards:
            return parsed_cards[0]
        return None

    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        all_products: list[ScrapedProduct] = []
        seen_skus: set[str] = set()

        for page in range(1, max(1, max_pages) + 1):
            page_url = _with_paris_page_param(category_url, page)
            html = await self.fetch_hybrid(page_url, wait_selector="[data-cnstrc-item-id]")
            parsed = self.parse_category_html(html, category_name=category_name)

            if not parsed and self.settings.playwright_fallback_enabled:
                logger.info(
                    "[Paris] Empty HTTP parse for %s (page %d); trying Playwright fallback.",
                    category_name,
                    page,
                )
                html = await self.fetch_browser(page_url, wait_selector="[data-cnstrc-item-id]")
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
            "[Paris] Scraped %d products from category '%s'",
            len(all_products),
            category_name,
        )
        return all_products

    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        html = await self.fetch_hybrid(product_url, wait_selector='script[type="application/ld+json"]')
        product = self.parse_product_html(
            html,
            product_url=product_url,
            category_name=category_name,
        )

        if product is None and self.settings.playwright_fallback_enabled:
            logger.info("[Paris] Empty HTTP product parse for %s; trying Playwright.", product_url)
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
